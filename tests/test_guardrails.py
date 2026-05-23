import pytest

from prr_pressure_cooker.adapters import OutboundActionBlocked, block_outbound


def test_outbound_actions_fail_closed():
    with pytest.raises(OutboundActionBlocked):
        block_outbound("send_email")
