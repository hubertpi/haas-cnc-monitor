import ipaddress
import re
from typing import Literal
from pydantic import BaseModel, Field, field_validator, ConfigDict


class Endpoint(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    protocol: Literal["mtconnect", "mdc"] = "mtconnect"
    device: str = Field(default="", max_length=128)
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(default=8082, ge=1, le=65535)
    timeout: int = Field(default=3, ge=1, le=10)

    @field_validator("device")
    @classmethod
    def device_name(cls, value):
        if any(ord(c)<32 or ord(c)==127 for c in value):
            raise ValueError("Use a printable MTConnect device name")
        return value.strip()

    @field_validator("host")
    @classmethod
    def address(cls, value):
        try:
            address = ipaddress.ip_address(value)
            if address.is_multicast or address.is_unspecified:
                raise ValueError("Use a specific machine address")
            return value
        except ValueError:
            if re.fullmatch(r"[0-9.]+", value):
                raise ValueError("Invalid IPv4 address")
            labels = value.split(".")
            if not all(re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", s) for s in labels):
                raise ValueError("Use an IP address or DNS hostname, without URL or path")
            return value.lower()


class MachineInput(Endpoint):
    name: str = Field(min_length=1, max_length=80)
    interval: int = Field(default=1, ge=1, le=60)
    enabled: bool = True
    revision: int | None = Field(default=None, ge=1)
    fast_interval: float = Field(default=1.0, ge=0.1, le=60)
    diagnostic_interval: int = Field(default=60, ge=5, le=600)
    boost_interval: float = Field(default=0.5, ge=0.1, le=1)
    boost_minutes: int = Field(default=10, ge=1, le=60)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value):
        value = value.strip()
        if not value or any(ord(ch) < 32 for ch in value):
            raise ValueError("Enter a printable machine name")
        return value


class ScanInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    network: str
    port: int = Field(default=8082, ge=1, le=65535)

    @field_validator("network")
    @classmethod
    def valid_network(cls, value):
        try:
            network = ipaddress.ip_network(value, strict=False)
        except ValueError:
            raise ValueError("Enter an IPv4 subnet such as 192.168.10.0/24")
        if network.version != 4 or network.prefixlen < 22 or network.is_multicast or network.is_unspecified:
            raise ValueError("Use a specific IPv4 subnet of /22 or smaller (up to 1,024 addresses)")
        if any(a.is_multicast or a.is_unspecified for a in (network.network_address, network.broadcast_address)):
            raise ValueError("Invalid discovery subnet")
        return str(network)


class BoostInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool
    minutes: int | None = Field(default=None, ge=1, le=60)
    interval: float | None = Field(default=None, ge=0.1, le=1)
