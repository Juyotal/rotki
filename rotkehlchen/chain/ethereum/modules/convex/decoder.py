import logging
from typing import Any, Callable

from rotkehlchen.accounting.structures.base import HistoryEventSubType, HistoryEventType
from rotkehlchen.chain.ethereum.modules.convex.constants import (
    BOOSTER,
    CONVEX_ABRAS_HEX,
    CONVEX_POOLS,
    CONVEX_VIRTUAL_REWARDS,
    CPT_CONVEX,
    CVX_LOCKER,
    CVX_LOCKER_V2,
    CVX_REWARDS,
    CVXCRV_REWARDS,
    REWARD_TOPICS,
    WITHDRAWAL_TOPICS,
)
from rotkehlchen.chain.ethereum.utils import asset_normalized_value
from rotkehlchen.chain.evm.constants import ZERO_ADDRESS
from rotkehlchen.chain.evm.decoding.constants import ERC20_OR_ERC721_TRANSFER
from rotkehlchen.chain.evm.decoding.interfaces import DecoderInterface
from rotkehlchen.chain.evm.decoding.structures import (
    DEFAULT_DECODING_OUTPUT,
    DEFAULT_ENRICHMENT_OUTPUT,
    DecoderContext,
    DecodingOutput,
    EnricherContext,
    TransferEnrichmentOutput,
)
from rotkehlchen.errors.asset import UnknownAsset, WrongAssetType
from rotkehlchen.logging import RotkehlchenLogsAdapter
from rotkehlchen.types import ChecksumEvmAddress
from rotkehlchen.utils.misc import hex_or_bytes_to_address, hex_or_bytes_to_int

logger = logging.getLogger(__name__)
log = RotkehlchenLogsAdapter(logger)


class ConvexDecoder(DecoderInterface):

    def _decode_convex_events(self, context: DecoderContext) -> DecodingOutput:
        amount_raw = hex_or_bytes_to_int(context.tx_log.data[0:32])
        interacted_address = hex_or_bytes_to_address(context.tx_log.topics[1])

        for event in context.decoded_events:
            try:
                crypto_asset = event.asset.resolve_to_crypto_asset()
            except (UnknownAsset, WrongAssetType):
                self.notify_user(event=event, counterparty=CPT_CONVEX)
                continue

            amount = asset_normalized_value(amount_raw, crypto_asset)
            if (
                event.location_label == context.transaction.from_address == interacted_address is False or  # noqa: E501
                (event.address != ZERO_ADDRESS and event.balance.amount != amount)
            ):
                continue
            if (
                event.event_type == HistoryEventType.SPEND and
                event.event_subtype == HistoryEventSubType.NONE
            ):
                if event.address == ZERO_ADDRESS:
                    event.event_subtype = HistoryEventSubType.RETURN_WRAPPED
                    event.counterparty = CPT_CONVEX
                    if context.tx_log.address in CONVEX_POOLS:
                        event.notes = f'Return {event.balance.amount} {crypto_asset.symbol} to convex {CONVEX_POOLS[context.tx_log.address]} pool'  # noqa: E501
                    else:
                        event.notes = f'Return {event.balance.amount} {crypto_asset.symbol} to convex'  # noqa: E501
                else:
                    event.event_type = HistoryEventType.DEPOSIT
                    event.counterparty = CPT_CONVEX
                    if context.tx_log.address in CONVEX_POOLS:
                        event.notes = f'Deposit {event.balance.amount} {crypto_asset.symbol} into convex {CONVEX_POOLS[context.tx_log.address]} pool'  # noqa: E501
                    else:
                        event.notes = f'Deposit {event.balance.amount} {crypto_asset.symbol} into convex'  # noqa: E501
            elif (
                event.event_type == HistoryEventType.RECEIVE and
                event.event_subtype == HistoryEventSubType.NONE
            ):
                if context.tx_log.topics[0] in WITHDRAWAL_TOPICS:
                    event.event_type = HistoryEventType.WITHDRAWAL
                    if context.tx_log.address in CONVEX_POOLS:
                        event.notes = f'Withdraw {event.balance.amount} {crypto_asset.symbol} from convex {CONVEX_POOLS[context.tx_log.address]} pool'  # noqa: E501
                    else:
                        event.notes = f'Withdraw {event.balance.amount} {crypto_asset.symbol} from convex'  # noqa: E501
                    event.counterparty = CPT_CONVEX
                elif context.tx_log.topics[0] in REWARD_TOPICS:
                    event.event_subtype = HistoryEventSubType.REWARD
                    event.counterparty = CPT_CONVEX
                    if context.tx_log.address in CONVEX_POOLS:
                        event.notes = f'Claim {event.balance.amount} {crypto_asset.symbol} reward from convex {CONVEX_POOLS[context.tx_log.address]} pool'  # noqa: E501
                    else:
                        event.notes = f'Claim {event.balance.amount} {crypto_asset.symbol} reward from convex'  # noqa: E501
        return DEFAULT_DECODING_OUTPUT

    @staticmethod
    def _maybe_enrich_convex_transfers(context: EnricherContext) -> TransferEnrichmentOutput:
        """
        Used for rewards paid with abracadabras. Problem is that the transfer event in this
        case happens at the end of the transaction and there is no reward event after it to
        emit event processing.

        May raise:
        - UnknownAsset
        - WrongAssetType
        """
        if (
            context.tx_log.topics[0] == ERC20_OR_ERC721_TRANSFER and
            context.tx_log.topics[1] in CONVEX_ABRAS_HEX and
            context.event.location_label == context.transaction.from_address and
            context.event.event_type == HistoryEventType.RECEIVE and
            context.event.event_subtype == HistoryEventSubType.NONE
        ):
            crypto_asset = context.event.asset.resolve_to_crypto_asset()
            context.event.event_subtype = HistoryEventSubType.REWARD
            if context.tx_log.address in CONVEX_POOLS:
                context.event.notes = f'Claim {context.event.balance.amount} {crypto_asset.symbol} reward from convex {CONVEX_POOLS[context.tx_log.address]} pool'  # noqa: E501
            else:
                context.event.notes = f'Claim {context.event.balance.amount} {crypto_asset.symbol} reward from convex'  # noqa: E501
            context.event.counterparty = CPT_CONVEX
            return DEFAULT_ENRICHMENT_OUTPUT
        return DEFAULT_ENRICHMENT_OUTPUT

    def addresses_to_decoders(self) -> dict[ChecksumEvmAddress, tuple[Any, ...]]:
        decoder_mappings: dict[ChecksumEvmAddress, tuple[Callable, ...]] = {
            BOOSTER: (self._decode_convex_events,),
            CVX_LOCKER: (self._decode_convex_events,),
            CVX_LOCKER_V2: (self._decode_convex_events,),
            CVX_REWARDS: (self._decode_convex_events,),
            CVXCRV_REWARDS: (self._decode_convex_events,),
        }
        pools = {pool: (self._decode_convex_events,) for pool in CONVEX_POOLS}
        virtual_rewards = {addr: (self._decode_convex_events,) for addr in CONVEX_VIRTUAL_REWARDS}
        decoder_mappings.update(pools)
        decoder_mappings.update(virtual_rewards)
        return decoder_mappings

    def counterparties(self) -> list[str]:
        return [CPT_CONVEX]

    def enricher_rules(self) -> list[Callable]:
        return [self._maybe_enrich_convex_transfers]
