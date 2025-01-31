import datetime

import pytest
from freezegun import freeze_time

from rotkehlchen.chain.ethereum.modules.yearn.utils import query_yearn_vaults
from rotkehlchen.chain.evm.types import string_to_evm_address
from rotkehlchen.constants.timing import WEEK_IN_SECONDS
from rotkehlchen.globaldb.cache import (
    globaldb_get_general_cache_last_queried_ts,
    globaldb_get_general_cache_values,
)
from rotkehlchen.globaldb.handler import GlobalDBHandler
from rotkehlchen.types import YEARN_VAULTS_V2_PROTOCOL, ChainID, GeneralCacheType


@pytest.mark.parametrize('globaldb_upgrades', [[]])
@pytest.mark.parametrize('run_globaldb_migrations', [False])
@pytest.mark.parametrize('custom_globaldb', ['v4_global_before_migration1.db'])
def test_yearn_api(database):
    """Test that vaults are queried correctly

    Since they are already in the globalDB we check with an older globalDB
    """
    with GlobalDBHandler().conn.read_ctx() as cursor:
        state_before = globaldb_get_general_cache_values(
            cursor=cursor,
            key_parts=[GeneralCacheType.YEARN_VAULTS],
        )

    query_yearn_vaults(database)

    with GlobalDBHandler().conn.read_ctx() as cursor:
        state_after = globaldb_get_general_cache_values(
            cursor=cursor,
            key_parts=[GeneralCacheType.YEARN_VAULTS],
        )

        last_queried_ts = globaldb_get_general_cache_last_queried_ts(
            cursor=cursor,
            key_parts=[GeneralCacheType.YEARN_VAULTS],
            value=str(state_after[0]),
        )
        assert last_queried_ts is not None

    assert state_after != state_before
    # 140 is the number of vaults at the moment of writing this test
    assert int(state_after[0]) > 140

    # check that a new vault was added
    token = GlobalDBHandler.get_evm_token(
        address=string_to_evm_address('0x341bb10D8f5947f3066502DC8125d9b8949FD3D6'),
        chain_id=ChainID.ETHEREUM,
    )

    assert token is not None
    assert token.name == 'yvCurve-STG-USDC 0.4.3'
    assert token.symbol == 'yvCurve-STG-USDC'
    assert token.protocol == YEARN_VAULTS_V2_PROTOCOL

    # trigger the query again and check that the timestamp was updated
    future_timestamp = datetime.datetime.now(tz=datetime.timezone.utc) + datetime.timedelta(seconds=WEEK_IN_SECONDS)  # noqa: E501
    with freeze_time(future_timestamp):
        query_yearn_vaults(database)

    with GlobalDBHandler().conn.read_ctx() as cursor:
        new_queried_ts = globaldb_get_general_cache_last_queried_ts(
            cursor=cursor,
            key_parts=[GeneralCacheType.YEARN_VAULTS],
            value=str(state_after[0]),
        )
    assert new_queried_ts is not None
    assert new_queried_ts > last_queried_ts
