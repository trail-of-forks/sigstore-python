# Copyright 2023 The Sigstore Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Rekor Checkpoint machinery.
"""

from __future__ import annotations

import base64
import re
import struct
from dataclasses import dataclass
from typing import List

from pydantic import BaseModel, Field, StrictStr

from sigstore._internal.keyring import KeyringSignatureError
from sigstore._internal.rekor.client import RekorClient
from sigstore._utils import KeyID


@dataclass(frozen=True)
class RekorSignature:
    """
    Represents a `RekorSignature` containing:
    - the name of the signature, e.g. "rekor.sigstage.dev"
    - the signature hash
    - the base64 signature
    """

    # FIXME(jl): this does not feel like a de novo definition...
    # does this exist already in sigstore-python (or its depenencices)?
    name: str
    sig_hash: bytes
    signature: bytes


class LogCheckpoint(BaseModel):
    """
    Represents a Rekor `LogCheckpoint` containing:
    - an origin, e.g. "rekor.sigstage.dev - 8050909264565447525"
    - the size of the log,
    - the hash of the log,
    - and any ancillary contants, e.g. "Timestamp: 1679349379012118479"
    see: https://github.com/transparency-dev/formats/blob/main/log/README.md
    """

    origin: StrictStr
    log_size: int
    log_hash: StrictStr
    other_content: List[str]

    @classmethod
    def from_text(cls, text: str) -> LogCheckpoint:
        """
        Serialize from the text header ("note") of a SignedNote.
        """

        lines = text.strip().split("\n")
        if len(lines) < 4:
            raise ValueError("Malformed LogCheckpoint: too few items in header!")

        origin = lines[0]
        if len(origin) == 0:
            raise ValueError("Malformed LogCheckpoint: empty origin!")

        log_size = int(lines[1])
        root_hash = base64.b64decode(lines[2]).hex()

        return LogCheckpoint(
            origin=origin,
            log_size=log_size,
            log_hash=root_hash,
            other_content=lines[3:],
        )

    @classmethod
    def to_text(self) -> str:
        """
        Serialize a `LogCheckpoint` into text format.
        See class definition for a prose description of the format.
        """
        return "\n".join(
            [
                self.origin,
                str(self.log_size),
                self.log_hash,
            ]
            + self.other_content
        )


class InvalidSignedNote(Exception):
    """
    Raised during SignedNote verification if invalid in some way.
    """

    pass


@dataclass(frozen=True)
class SignedNote:
    """
    Represents a signed `Note` containing a note and its corresponding list of signatures.
    """

    note: StrictStr = Field(..., alias="note")
    signatures: list[RekorSignature] = Field(..., alias="signatures")

    @classmethod
    def from_text(cls, text: str) -> SignedNote:
        """
        Serialize from a bundled text 'note'.

        A note contains:
        - a name, a string associated with the signer,
        - a separator blank line,
        - and signature(s), each signature takes the form
            `\u2014 NAME SIGNATURE\n`
          (where \u2014 == em dash).

        An adaptation of the Rekor's `UnmarshalText`:
        https://github.com/sigstore/rekor/blob/4b1fa6661cc6dfbc844b4c6ed9b1f44e7c5ae1c0/pkg/util/signed_note.go#L141
        """

        separator: str = "\n\n"
        if text.count(separator) != 1:
            raise ValueError(
                "Note must contain one blank line, deliniating the text from the signature block"
            )
        split = text.index(separator)

        header: str = text[: split + 1]
        data: str = text[split + len(separator) :]

        if len(data) == 0:
            raise ValueError("Malformed Note: must contain at least one signature!")
        if data[-1] != "\n":
            raise ValueError("Malformed Note: data section must end with newline!")

        sig_parser = re.compile(r"\u2014 (\S+) (\S+)\n")
        signatures: list[RekorSignature] = []
        for name, signature in re.findall(sig_parser, data):
            signature_bytes: bytes = base64.b64decode(signature)
            if len(signature_bytes) < 5:
                raise ValueError("Malformed Note: signature contains too few bytes")

            signature = RekorSignature(
                name=name,
                # FIXME(jl): In Go, construct a big-endian UInt32 from 4 bytes. Is this equivalent?
                sig_hash=struct.unpack(">4s", signature_bytes[0:4])[0],
                signature=base64.b64encode(signature_bytes[4:]),
            )
            signatures.append(signature)

        return cls(note=header, signatures=signatures)

    def verify(self, client: RekorClient, key_id: KeyID) -> None:
        note = str.encode(self.note)

        for sig in self.signatures:
            if sig.sig_hash != key_id[:4]:
                raise InvalidSignedNote("sig_hash hint does not match expected key_id")

            try:
                client._rekor_keyring.verify(
                    key_id=key_id, signature=base64.b64decode(sig.signature), data=note
                )
            except KeyringSignatureError as sig_err:
                raise InvalidSignedNote("invalid signature") from sig_err


@dataclass(frozen=True)
class SignedCheckpoint:
    """
    Represents a *signed* `Checkpoint`: a `LogCheckpoint` and its corresponding *signed* `Note`.
    """

    signed_note: SignedNote
    checkpoint: LogCheckpoint

    @classmethod
    def from_text(cls, text: str) -> SignedCheckpoint:
        """
        Create a new `SignedCheckpoint` from the text representation.
        """

        signed_note = SignedNote.from_text(text)
        checkpoint = LogCheckpoint.from_text(signed_note.note)
        return cls(signed_note=signed_note, checkpoint=checkpoint)