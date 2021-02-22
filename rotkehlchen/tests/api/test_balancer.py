import random
import warnings as test_warnings
from contextlib import ExitStack
from http import HTTPStatus

import pytest
import requests

from rotkehlchen.constants import ZERO
from rotkehlchen.fval import FVal
from rotkehlchen.premium.premium import Premium
from rotkehlchen.serialization.deserialize import deserialize_ethereum_address
from rotkehlchen.tests.utils.api import (
    api_url_for,
    assert_error_response,
    assert_ok_async_response,
    assert_proper_response_with_result,
    wait_for_async_task,
)
from rotkehlchen.tests.utils.rotkehlchen import setup_balances

# Top holder of WBTC-WETH pool (0x1eff8af5d577060ba4ac8a29a13525bb0ee2a3d5)
BALANCER_TEST_ADDR1 = deserialize_ethereum_address('0x49a2DcC237a65Cc1F412ed47E0594602f6141936')


@pytest.mark.parametrize('ethereum_accounts', [[BALANCER_TEST_ADDR1]])
@pytest.mark.parametrize('ethereum_modules', [['uniswap']])
def test_get_balances_module_not_activated(
        rotkehlchen_api_server,
        ethereum_accounts,  # pylint: disable=unused-argument
):
    response = requests.get(
        api_url_for(rotkehlchen_api_server, 'balancerbalancesresource'),
    )
    assert_error_response(
        response=response,
        contained_in_msg='balancer module is not activated',
        status_code=HTTPStatus.CONFLICT,
    )


@pytest.mark.parametrize('ethereum_accounts', [[BALANCER_TEST_ADDR1]])
@pytest.mark.parametrize('ethereum_modules', [['balancer']])
@pytest.mark.parametrize('start_with_valid_premium', [True])
def test_get_balances_premium(
        rotkehlchen_api_server,
        ethereum_accounts,  # pylint: disable=unused-argument
        rotki_premium_credentials,  # pylint: disable=unused-argument
        start_with_valid_premium,  # pylint: disable=unused-argument
):
    """Test get the balances for premium users works as expected"""
    async_query = random.choice([False, True])
    async_query = False
    rotki = rotkehlchen_api_server.rest_api.rotkehlchen

    # Set module premium is required for calling `get_balances()`
    premium = None
    if start_with_valid_premium:
        premium = Premium(rotki_premium_credentials)

    balancer = rotki.chain_manager.get_module('balancer')
    balancer.premium = premium

    setup = setup_balances(
        rotki,
        ethereum_accounts=ethereum_accounts,
        btc_accounts=None,
        original_queries=['zerion', 'logs', 'blocknobytime'],
    )
    with ExitStack() as stack:
        # patch ethereum/etherscan to not autodetect tokens
        setup.enter_ethereum_patches(stack)
        response = requests.get(api_url_for(
            rotkehlchen_api_server, 'balancerbalancesresource'),
            json={'async_query': async_query},
        )
        if async_query:
            task_id = assert_ok_async_response(response)
            outcome = wait_for_async_task(rotkehlchen_api_server, task_id)
            assert outcome['message'] == ''
            result = outcome['result']
        else:
            result = assert_proper_response_with_result(response)

    if len(result) != 1:
        test_warnings.warn(
            UserWarning(f'Test account {BALANCER_TEST_ADDR1} has no balances'),
        )
        return

    for pool_share in result[BALANCER_TEST_ADDR1]:
        assert pool_share['address'] is not None
        assert FVal(pool_share['total_amount']) >= ZERO
        assert FVal(pool_share['user_balance']['amount']) >= ZERO
        assert FVal(pool_share['user_balance']['usd_value']) >= ZERO

        for pool_token in pool_share['tokens']:
            assert pool_token['token'] is not None
            # UnknownEthereumToken
            if isinstance(pool_token['token'], dict):
                assert pool_token['token']['ethereum_address'] is not None
                assert pool_token['token']['name'] is not None
                assert pool_token['token']['symbol'] is not None

            assert pool_token['total_amount'] is not None
            assert FVal(pool_token['user_balance']['amount']) >= ZERO
            assert FVal(pool_token['user_balance']['usd_value']) >= ZERO
            assert FVal(pool_token['usd_price']) >= ZERO
            assert FVal(pool_token['weight']) >= ZERO
