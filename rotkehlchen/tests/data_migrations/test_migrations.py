import json
import operator
from contextlib import ExitStack
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple
from unittest.mock import patch

import pytest

from rotkehlchen.constants import ONE
from rotkehlchen.constants.assets import A_BTC, A_ETH
from rotkehlchen.data_migrations.manager import (
    LAST_DATA_MIGRATION,
    MIGRATION_LIST,
    DataMigrationManager,
    MigrationRecord,
)
from rotkehlchen.db.dbhandler import DBHandler
from rotkehlchen.fval import FVal
from rotkehlchen.icons import IconManager
from rotkehlchen.tests.utils.blockchain import setup_evm_addresses_activity_mock
from rotkehlchen.tests.utils.exchanges import check_saved_events_for_exchange
from rotkehlchen.tests.utils.factories import make_evm_address
from rotkehlchen.types import ChecksumEvmAddress, Location, SupportedBlockchain, TradeType

if TYPE_CHECKING:
    from rotkehlchen.api.server import APIServer
    from rotkehlchen.rotkehlchen import Rotkehlchen
    from rotkehlchen.tests.fixtures.websockets import WebsocketReader


def _create_invalid_icon(icon_identifier: str, icons_dir: Path) -> Path:
    icon_filepath = icons_dir / f'{icon_identifier}_small.png'
    with open(icon_filepath, 'wb') as f:
        f.write(b'abcd')

    return icon_filepath


class MockDataForMigrations(NamedTuple):
    db: DBHandler


class MockRotkiForMigrations:

    def __init__(self, db) -> None:
        self.data = MockDataForMigrations(db=db)
        self.msg_aggregator = db.msg_aggregator


@pytest.mark.parametrize('use_custom_database', ['data_migration_v0.db'])
@pytest.mark.parametrize('data_migration_version', [0])
@pytest.mark.parametrize('perform_upgrades_at_unlock', [False])
@pytest.mark.parametrize('new_db_unlock_actions', [None])
def test_migration_1(database):
    """
    Test that the first data migration for rotki works. This migration removes information about
    some exchanges when there is more than one instance or if it is kraken.

    In the test we setup instances of the exchanges to trigger the updates and one exchange
    (POLONIEX) that shouldn't be affected.
    """
    rotki = MockRotkiForMigrations(database)
    for exchange_location in [Location.BINANCE, Location.KRAKEN, Location.POLONIEX]:
        check_saved_events_for_exchange(
            exchange_location=exchange_location,
            db=database,
            should_exist=True,
            queryrange_formatstr='{exchange}_{type}',
        )
        cursor = database.conn.cursor()
        result = cursor.execute(
            'SELECT COUNT(*) from used_query_ranges WHERE name="bittrex_trades"',
        )
        assert result.fetchone()[0] == 1

    # Migration shouldn't execute and information should stay in database
    with database.user_write() as write_cursor:
        for exchange_location in [Location.BINANCE, Location.KRAKEN]:
            trade_tuples = ((
                f'custom-trade-id-{exchange_location}',
                1,
                exchange_location.serialize_for_db(),
                A_BTC.identifier,
                A_ETH.identifier,
                TradeType.BUY.serialize_for_db(),
                str(ONE),
                str(ONE),
                str(FVal('0.1')),
                A_ETH.identifier,
                'foo',
                'boo',
            ),)
            query = """
                INSERT INTO trades(
                id,
                time,
                location,
                base_asset,
                quote_asset,
                type,
                amount,
                rate,
                fee,
                fee_currency,
                link,
                notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
            database.write_tuples(write_cursor=write_cursor, tuple_type='trade', query=query, tuples=trade_tuples)  # noqa: E501
            database.update_used_query_range(write_cursor=write_cursor, name=f'{str(exchange_location)}_trades_{str(exchange_location)}', start_ts=0, end_ts=9999)  # noqa: E501
            database.update_used_query_range(write_cursor=write_cursor, name=f'{str(exchange_location)}_margins_{str(exchange_location)}', start_ts=0, end_ts=9999)  # noqa: E501
            database.update_used_query_range(write_cursor=write_cursor, name=f'{str(exchange_location)}_asset_movements_{str(exchange_location)}', start_ts=0, end_ts=9999)  # noqa: E501

    migration_patch = patch(
        'rotkehlchen.data_migrations.manager.MIGRATION_LIST',
        new=[MIGRATION_LIST[0]],
    )
    with migration_patch:
        DataMigrationManager(rotki).maybe_migrate_data()
    errors = rotki.msg_aggregator.consume_errors()
    warnings = rotki.msg_aggregator.consume_warnings()
    assert len(errors) == 0
    assert len(warnings) == 0
    check_saved_events_for_exchange(Location.BINANCE, rotki.data.db, should_exist=False)
    check_saved_events_for_exchange(Location.POLONIEX, rotki.data.db, should_exist=True)
    check_saved_events_for_exchange(Location.KRAKEN, rotki.data.db, should_exist=False)
    with database.conn.read_ctx() as cursor:
        assert rotki.data.db.get_settings(cursor).last_data_migration == LAST_DATA_MIGRATION


@pytest.mark.parametrize('use_custom_database', ['data_migration_v0.db'])
@pytest.mark.parametrize('perform_upgrades_at_unlock', [False])
@pytest.mark.parametrize('data_migration_version', [0])
@pytest.mark.parametrize('new_db_unlock_actions', [None])
def test_failed_migration(database):
    """Test that a failed migration does not update DB setting and logs error"""
    rotki = MockRotkiForMigrations(database)

    def botched_migration(rotki, progress_handler) -> None:
        raise ValueError('ngmi')

    botched_list = [MigrationRecord(version=1, function=botched_migration)]

    migrate_mock = patch(
        'rotkehlchen.data_migrations.manager.MIGRATION_LIST',
        new=botched_list,
    )

    # Ignore websocket messages with notifications about db upgrade version.
    # By default they will be treated as errors since we have no websocket connection set up.
    rotki.msg_aggregator.consume_errors()

    with migrate_mock:
        DataMigrationManager(rotki).maybe_migrate_data()

    with database.conn.read_ctx() as cursor:
        settings = database.get_settings(cursor)
    assert settings.last_data_migration == 0, 'no migration should have happened'
    errors = rotki.msg_aggregator.consume_errors()
    warnings = rotki.msg_aggregator.consume_warnings()
    assert len(warnings) == 0
    assert len(errors) == 1
    assert errors[0] == 'Failed to run soft data migration to version 1 due to ngmi'


@pytest.mark.parametrize('data_migration_version', [2])
@pytest.mark.parametrize('perform_upgrades_at_unlock', [False])
@pytest.mark.parametrize('use_clean_caching_directory', [True])
def test_migration_3(database, data_dir):
    """
    Test that the third data migration for rotki works. This migration removes icons of assets
    that are not valid images and update the list of ignored assets.
    """
    rotki = MockRotkiForMigrations(database)
    icon_manager = IconManager(data_dir=data_dir, coingecko=None)
    rotki.icon_manager = icon_manager
    btc_iconpath = _create_invalid_icon(A_BTC.identifier, icon_manager.icons_dir)
    eth_iconpath = _create_invalid_icon(A_ETH.identifier, icon_manager.icons_dir)

    migration_patch = patch(
        'rotkehlchen.data_migrations.manager.MIGRATION_LIST',
        new=MIGRATION_LIST[2:],
    )
    with migration_patch:
        DataMigrationManager(rotki).maybe_migrate_data()

    assert btc_iconpath.is_file() is False
    assert eth_iconpath.is_file() is False


@pytest.mark.parametrize('data_migration_version', [3])
@pytest.mark.parametrize('perform_upgrades_at_unlock', [False])
def test_migration_4(database, blockchain):
    """
    Test that the fourth data migration for rotki works. This migration adds the ethereum nodes
    that will be used as open nodes to the database.
    """
    rotki = MockRotkiForMigrations(database)
    rotki.chains_aggregator = blockchain
    migration_patch = patch(
        'rotkehlchen.data_migrations.manager.MIGRATION_LIST',
        new=MIGRATION_LIST[3:],
    )
    # Manually insert the old rpc setting in the table
    with database.user_write() as cursor:
        cursor.execute(
            'INSERT INTO settings(name, value) VALUES ("eth_rpc_endpoint", "https://localhost:5222");',  # noqa: E501
        )
    with migration_patch:
        DataMigrationManager(rotki).maybe_migrate_data()
    dir_path = Path(__file__).resolve().parent.parent.parent
    with database.conn.read_ctx() as cursor:
        cursor.execute(
            'SELECT * from settings where name=?', ('eth_rpc_endpoint',),
        )
        assert cursor.fetchone() is None, 'Setting should have been deleted'

    with open(dir_path / 'data' / 'nodes.json') as f:
        nodes = json.loads(f.read())
        rpc_nodes = database.get_rpc_nodes(blockchain=SupportedBlockchain.ETHEREUM)
        rpc_nodes += database.get_rpc_nodes(blockchain=SupportedBlockchain.OPTIMISM)
        assert len(rpc_nodes) >= len(nodes) + 1
        owned_index = -1
        for node in nodes:
            for idx, rpc_node in enumerate(rpc_nodes):
                if rpc_node.node_info.name == node['name']:
                    assert rpc_node.node_info.endpoint == node['endpoint']
                    assert rpc_node.active == node['active']
                    assert rpc_node.node_info.owned == node['owned']
                    continue
                if rpc_node.node_info.owned is True:
                    owned_index = idx

        assert len(rpc_nodes) >= 5
        assert rpc_nodes[owned_index].node_info.owned is True
        assert rpc_nodes[owned_index].node_info.endpoint == 'https://localhost:5222'


@pytest.mark.parametrize('data_migration_version', [3])
@pytest.mark.parametrize('perform_upgrades_at_unlock', [False])
def test_migration_4_no_own_endpoint(database, blockchain):
    """
    Test that the fourth data migration for rotki works when there is no custom node
    """
    rotki = MockRotkiForMigrations(database)
    rotki.chains_aggregator = blockchain
    migration_patch = patch(
        'rotkehlchen.data_migrations.manager.MIGRATION_LIST',
        new=MIGRATION_LIST[3:],
    )
    with migration_patch:
        DataMigrationManager(rotki).maybe_migrate_data()
    dir_path = Path(__file__).resolve().parent.parent.parent
    with database.conn.read_ctx() as cursor:
        cursor.execute(
            'SELECT * from settings where name=?', ('eth_rpc_endpoint',),
        )
        assert cursor.fetchone() is None, 'Setting should have been deleted'
    rpc_nodes = database.get_rpc_nodes(blockchain=SupportedBlockchain.ETHEREUM)
    rpc_nodes += database.get_rpc_nodes(blockchain=SupportedBlockchain.OPTIMISM)
    with open(dir_path / 'data' / 'nodes.json') as f:
        nodes = json.loads(f.read())
        assert len(rpc_nodes) >= len(nodes)


@pytest.mark.parametrize('data_migration_version', [4])
@pytest.mark.parametrize('perform_upgrades_at_unlock', [False])
@pytest.mark.parametrize('use_clean_caching_directory', [True])
def test_migration_5(database, data_dir):
    """
    Test that the fifth data migration for rotki works.
    - Create two fake icons and check that the file name was correctly updated
    """
    rotki = MockRotkiForMigrations(database)
    icon_manager = IconManager(data_dir=data_dir, coingecko=None)
    rotki.icon_manager = icon_manager
    migration_patch = patch(
        'rotkehlchen.data_migrations.manager.MIGRATION_LIST',
        new=MIGRATION_LIST[4:],
    )
    # Create some fake icon files
    icons_path = icon_manager.icons_dir
    Path(icons_path, '_ceth_0x6c3F90f043a72FA612cbac8115EE7e52BDe6E490_small.png').touch()
    Path(icons_path, '_ceth_0x6DEA81C8171D0bA574754EF6F8b412F2Ed88c54D_small.png').touch()
    Path(icons_path, '_ceth_0x7Fc66500c84A76Ad7e9c93437bFc5Ac33E2DDaE9_small.png').touch()
    Path(icons_path, 'eip155%3A1%2Ferc20%3A0x7Fc66500c84A76Ad7e9c93437bFc5Ac33E2DDaE9_small.png').touch()  # noqa: E501
    # the two files + the custom assets folder
    assert len(list(icon_manager.icons_dir.iterdir())) == 5
    with migration_patch:
        DataMigrationManager(rotki).maybe_migrate_data()

    assert Path(icons_path, 'eip155%3A1%2Ferc20%3A0x6c3F90f043a72FA612cbac8115EE7e52BDe6E490_small.png').is_file() is True  # noqa: E501
    assert Path(icons_path, 'eip155%3A1%2Ferc20%3A0x6DEA81C8171D0bA574754EF6F8b412F2Ed88c54D_small.png').is_file() is True  # noqa: E501
    assert Path(icons_path, 'eip155%3A1%2Ferc20%3A0x6DEA81C8171D0bA574754EF6F8b412F2Ed88c54D_small.png').is_file() is True  # noqa: E501
    assert Path(icons_path, '_ceth_0x6c3F90f043a72FA612cbac8115EE7e52BDe6E490_small.png').exists() is False  # noqa: E501
    assert Path(icons_path, '_ceth_0x6DEA81C8171D0bA574754EF6F8b412F2Ed88c54D_small.png').exists() is False  # noqa: E501
    assert Path(icons_path, '_ceth_0x7Fc66500c84A76Ad7e9c93437bFc5Ac33E2DDaE9_small.png').exists() is False  # noqa: E501


def _get_nodes():
    """Reads old (as of 1.26.1) and new nodes from json files and returns them

    These tests ignore any non ETHEREUM nodes
    """
    dir_path = Path(__file__).resolve().parent.parent.parent
    with open(dir_path / 'data' / 'nodes_as_of_1-26-1.json') as f:
        old_nodes_info = json.loads(f.read())
    old_nodes = [
        [node['name'], node['endpoint'], node['owned'], str(node['weight']), node['active'], node['blockchain']]  # noqa: E501
        for node in old_nodes_info
    ]
    with open(dir_path / 'data' / 'nodes.json') as f:
        new_nodes_info = json.loads(f.read())
    new_nodes = {
        (node['name'], node['endpoint'], node['owned'], str(node['weight']), node['blockchain'])  # noqa: E501
        for node in new_nodes_info if node['blockchain'] == 'ETH'
    }
    return old_nodes, new_nodes


def _write_nodes_and_migrate(
        database: 'DBHandler',
        rotki: 'Rotkehlchen',
        nodes_to_write: list[tuple[str, str, bool, str, bool, str]],
) -> list[tuple[str, str, bool, str, bool, str]]:
    """
    Writes the given nodes to the DB and applies 6th migration.
    Returns nodes from the DB after the migration.
    """
    with database.user_write() as write_cursor:
        write_cursor.executemany(
            'INSERT INTO rpc_nodes (name, endpoint, owned, weight, active, blockchain) VALUES (?, ?, ?, ?, ?, ?)',  # noqa: E501
            nodes_to_write,
        )
    migration_patch = patch(
        'rotkehlchen.data_migrations.manager.MIGRATION_LIST',
        new=[MIGRATION_LIST[5]],
    )
    with migration_patch:
        DataMigrationManager(rotki).maybe_migrate_data()

    # Now nodes should be the new defaults
    with database.conn.read_ctx() as cursor:
        nodes_in_db = cursor.execute('SELECT name, endpoint, owned, weight, blockchain FROM rpc_nodes').fetchall()  # noqa: E501

    return nodes_in_db


@pytest.mark.parametrize('data_migration_version', [5])
@pytest.mark.parametrize('perform_upgrades_at_unlock', [False])
@pytest.mark.parametrize('new_db_unlock_actions', [None])
def test_migration_6_default_rpc_nodes(database):
    """
    Test that the sixth data migration works when the user has not customized the nodes.
    """
    rotki = MockRotkiForMigrations(database)
    old_nodes, new_nodes = _get_nodes()
    nodes_in_db = _write_nodes_and_migrate(database, rotki, old_nodes)

    # Now nodes should be the new defaults
    assert set(nodes_in_db) == new_nodes
    assert FVal(sum(FVal(node[3]) for node in nodes_in_db)) == ONE


@pytest.mark.parametrize('data_migration_version', [5])
@pytest.mark.parametrize('perform_upgrades_at_unlock', [False])
@pytest.mark.parametrize('new_db_unlock_actions', [None])
def test_migration_6_customized_rpc_nodes(database):
    """
    Test that the sixth data migration works when the user has customized the rpc nodes.
    """
    rotki = MockRotkiForMigrations(database)
    old_nodes, _ = _get_nodes()
    old_nodes[-1][0] = 'Renamed cloudflare'  # customize a node
    # Also add a new node
    old_nodes.append(('flashbots', 'https://rpc.flashbots.net/', False, '0.1', True, 'ETH'))

    nodes_in_db = _write_nodes_and_migrate(database, rotki, old_nodes)

    # Create the expected list of nodes. Check that the customized node is still there, all dead
    # nodes were removed and that the nodes were reweighed properly.
    expected_nodes = {
        ('etherscan', '', False, '0.6', 'ETH'),
        ('Renamed cloudflare', 'https://cloudflare-eth.com/', False, '0.2', 'ETH'),
        ('flashbots', 'https://rpc.flashbots.net/', False, '0.2', 'ETH'),
    }

    assert set(nodes_in_db) == expected_nodes


@pytest.mark.parametrize('data_migration_version', [5])
@pytest.mark.parametrize('perform_upgrades_at_unlock', [False])
@pytest.mark.parametrize('new_db_unlock_actions', [None])
def test_migration_6_own_node(database):
    """
    Test that the sixth data migration works when user has no customized nodes but has
    an own node set.
    """
    rotki = MockRotkiForMigrations(database)
    old_nodes, new_nodes = _get_nodes()
    # Add an owned node
    old_nodes.append(('Owned node', 'http://localhost:8045', True, '1', True, 'ETH'))

    nodes_in_db = _write_nodes_and_migrate(database, rotki, old_nodes)
    # Create the expected list of nodes. Check that the owned node is still there and that the
    # defaults were replaced.
    expected_nodes = {*new_nodes, ('Owned node', 'http://localhost:8045', True, '1', 'ETH')}
    assert set(nodes_in_db) == expected_nodes


@pytest.mark.parametrize('data_migration_version', [6])
@pytest.mark.parametrize('perform_upgrades_at_unlock', [False])
def test_migration_7_nodes(database, blockchain, greenlet_manager):
    """
    Test that the seventh data migration works adding llamanode to the list of eth nodes.
    """
    rotki = MockRotkiForMigrations(database)
    rotki.chains_aggregator = blockchain
    migrate_mock = patch(
        'rotkehlchen.data_migrations.manager.MIGRATION_LIST',
        new=MIGRATION_LIST[3:7],
    )
    with migrate_mock:
        DataMigrationManager(rotki).maybe_migrate_data()

    # check that the task to connect the ethereum node is spawned
    expected_greenlet_name = 'Attempt connection to LlamaNodes ethereum node'
    assert any(expected_greenlet_name in greenlet.task_name for greenlet in greenlet_manager.greenlets) is True  # noqa: E501

    nodes = database.get_rpc_nodes(blockchain=SupportedBlockchain.ETHEREUM)
    # check that weight is correct for nodes
    assert sum(node.weight for node in nodes) == ONE
    llama_node_in_db = False
    for node in nodes:
        if node.node_info.endpoint == 'https://eth.llamarpc.com':
            llama_node_in_db = True
            break
    assert llama_node_in_db is True


@pytest.mark.parametrize('data_migration_version', [7])
@pytest.mark.parametrize('perform_upgrades_at_unlock', [False])
@pytest.mark.parametrize('ethereum_accounts', [[make_evm_address(), make_evm_address(), make_evm_address(), make_evm_address()]])  # noqa: E501
@pytest.mark.parametrize('legacy_messages_via_websockets', [True])
def test_migration_8(
        rotkehlchen_api_server: 'APIServer',
        ethereum_accounts: list[ChecksumEvmAddress],
        websocket_connection: 'WebsocketReader',
) -> None:
    """
    Test that accounts are properly duplicated from ethereum to optimism and avalanche
    """
    rotki = rotkehlchen_api_server.rest_api.rotkehlchen
    migration_patch = patch(
        'rotkehlchen.data_migrations.manager.MIGRATION_LIST',
        new=[MIGRATION_LIST[7]],
    )
    avalanche_addresses = [ethereum_accounts[1], ethereum_accounts[3]]
    optimism_addresses = [ethereum_accounts[2], ethereum_accounts[3]]

    with ExitStack() as stack:
        setup_evm_addresses_activity_mock(
            stack=stack,
            chains_aggregator=rotki.chains_aggregator,
            eth_contract_addresses=[ethereum_accounts[0]],
            ethereum_addresses=[],
            avalanche_addresses=avalanche_addresses,
            optimism_addresses=optimism_addresses,
        )
        stack.enter_context(migration_patch)
        DataMigrationManager(rotki).maybe_migrate_data()

    with rotki.data.db.conn.read_ctx() as cursor:  # make sure DB is also written
        accounts = rotki.data.db.get_blockchain_accounts(cursor)

    assert set(accounts.eth) == set(ethereum_accounts)
    assert set(rotki.chains_aggregator.accounts.eth) == set(ethereum_accounts)
    assert set(accounts.avax) == set(avalanche_addresses)
    assert set(rotki.chains_aggregator.accounts.avax) == set(avalanche_addresses)
    assert set(accounts.optimism) == set(optimism_addresses)
    assert set(rotki.chains_aggregator.accounts.optimism) == set(optimism_addresses)

    def assert_progress_message(msg, step_num, description) -> None:
        assert msg['type'] == 'data_migration_status'
        assert msg['data']['start_version'] == 8
        assert msg['data']['target_version'] == LAST_DATA_MIGRATION
        migration = msg['data']['current_migration']
        assert migration['version'] == 8
        assert migration['total_steps'] == (5 if step_num != 0 else 0)
        assert migration['current_step'] == step_num
        if 1 <= step_num <= 4:
            assert 'EVM chain activity' in migration['description']
        else:
            assert migration['description'] == description

    websocket_connection.wait_until_messages_num(num=7, timeout=10)
    assert websocket_connection.messages_num() == 7
    for i in range(7):
        msg = websocket_connection.pop_message()
        if i == 6:  # message for migrated address
            assert msg['type'] == 'evm_accounts_detection'
            assert sorted(msg['data'], key=operator.itemgetter('evm_chain', 'address')) == sorted([
                {'evm_chain': 'avalanche', 'address': ethereum_accounts[1]},
                {'evm_chain': 'avalanche', 'address': ethereum_accounts[3]},
                {'evm_chain': 'optimism', 'address': ethereum_accounts[2]},
                {'evm_chain': 'optimism', 'address': ethereum_accounts[3]},
            ], key=operator.itemgetter('evm_chain', 'address'))
        elif i >= 5:
            assert_progress_message(msg, i, 'Potentially write migrated addresses to the DB')
        elif i >= 1:
            assert_progress_message(msg, i, None)
        else:
            assert_progress_message(msg, i, None)


@pytest.mark.parametrize('use_custom_database', ['data_migration_9.db'])
@pytest.mark.parametrize('perform_upgrades_at_unlock', [False])
# make sure fixtures does not modify DB last_data_migration
@pytest.mark.parametrize('data_migration_version', [None])
def test_migration_9(database) -> None:
    """This is a multifaceted test that tests multiple things:

    1. Makes sure that if a new account was created with 1.27.0 and is opened later
    with 1.27.1 it runs migration 9.
    2. Make sure that migration 9 works properly
    """
    rotki = MockRotkiForMigrations(database)
    # Check DB before migration
    with rotki.data.db.conn.read_ctx() as cursor:
        cursor.execute('SELECT COUNT(*) from location WHERE location IN ("f", "g") ')
        assert cursor.fetchone()[0] == 0
        cursor.execute('SELECT COUNT(*) from history_events')
        total_history_events = cursor.fetchone()[0]
        assert total_history_events == 242
        cursor.execute('SELECT location from history_events')
        assert all([x[0] == 'J'] for x in cursor)
        cursor.execute('SELECT COUNT(*) from history_events_mappings WHERE name="chain_id"')
        assert cursor.fetchone()[0] == total_history_events
        cursor.execute('SELECT COUNT(*) from evm_events_info WHERE counterparty="curve"')
        curve_events = cursor.fetchone()[0]
        assert curve_events == 5

    migration_patch = patch(
        'rotkehlchen.data_migrations.manager.MIGRATION_LIST',
        new=[MIGRATION_LIST[8]],
    )
    with migration_patch:
        DataMigrationManager(rotki).maybe_migrate_data()  # type: ignore  # it's a mock

    # And now assert migration worked fine
    errors = rotki.msg_aggregator.consume_errors()
    warnings = rotki.msg_aggregator.consume_warnings()
    assert len(errors) == 0
    assert len(warnings) == 0
    with rotki.data.db.conn.read_ctx() as cursor:
        cursor.execute('SELECT COUNT(*) from location WHERE location IN ("f", "g") ')
        assert cursor.fetchone()[0] == 2
        cursor.execute('SELECT COUNT(*) from history_events')
        assert cursor.fetchone()[0] == total_history_events - curve_events
        cursor.execute('SELECT location from history_events')
        assert all([x[0] in ('f', 'g')] for x in cursor)
        cursor.execute('SELECT COUNT(*) from history_events_mappings WHERE name="chain_id"')
        assert cursor.fetchone()[0] == 0


@pytest.mark.parametrize('perform_upgrades_at_unlock', [False])
# make sure fixtures does not modify DB last_data_migration
@pytest.mark.parametrize('data_migration_version', [None])
def test_new_db_remembers_last_migration_even_if_no_migrations_run(database):
    """Test that a newly created database remembers the current last data migration
    at the time of creation even if no migration has actually ran"""
    rotki = MockRotkiForMigrations(database)
    with rotki.data.db.conn.read_ctx() as cursor:
        cursor.execute('SELECT value FROM settings WHERE name="last_data_migration"')
        assert cursor.fetchone() is None

    migration_patch = patch(
        'rotkehlchen.data_migrations.manager.MIGRATION_LIST',
        new=[],
    )
    with migration_patch:
        DataMigrationManager(rotki).maybe_migrate_data()

    with rotki.data.db.conn.read_ctx() as cursor:
        cursor.execute('SELECT value FROM settings WHERE name="last_data_migration"')
        assert int(cursor.fetchone()[0]) == LAST_DATA_MIGRATION
