import uuid

from rk.runtime import Uuid7Generator


def test_uuid7_has_expected_version_and_variant() -> None:
    generator = Uuid7Generator(time_ns=lambda: 1_700_000_000_000_000_000, randbits=lambda n: 1)
    value = uuid.UUID(generator.new())

    assert value.version == 7
    assert value.variant == uuid.RFC_4122
