import datetime
from functools import partial
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import stripe as stripe_lib
from pytest_mock import MockerFixture
from sqlalchemy import select

from polar.config import settings
from polar.enums import AccountType
from polar.integrations.stripe.service import StripeService
from polar.kit.utils import utc_now
from polar.locker import Locker
from polar.models import Account, Organization, Transaction, User
from polar.models.transaction import Processor, TransactionType
from polar.postgres import AsyncSession
from polar.transaction.service.payout import (
    InsufficientBalance,
    NotReadyAccount,
    StripePayoutNotPaid,
    UnderReviewAccount,
    UnknownAccount,
)
from polar.transaction.service.payout import (
    payout_transaction as payout_transaction_service,
)
from tests.fixtures import random_objects as ro
from tests.fixtures.database import SaveFixture
from tests.transaction.conftest import (
    create_account,
    create_async_iterator,
    create_transaction,
)


@pytest.fixture(autouse=True)
def stripe_service_mock(mocker: MockerFixture) -> MagicMock:
    mock = MagicMock(spec=StripeService)
    mocker.patch("polar.transaction.service.payout.stripe_service", new=mock)
    return mock


create_payment_transaction = partial(ro.create_payment_transaction, amount=10000)
create_refund_transaction = partial(ro.create_refund_transaction, amount=-10000)
create_balance_transaction = partial(ro.create_balance_transaction, amount=10000)


@pytest.mark.asyncio
class TestCreatePayout:
    @pytest.mark.parametrize(
        "balance", [-1000, 0, settings.ACCOUNT_PAYOUT_MINIMUM_BALANCE - 1]
    )
    async def test_insufficient_balance(
        self,
        balance: int,
        session: AsyncSession,
        locker: Locker,
        save_fixture: SaveFixture,
        user: User,
    ) -> None:
        account = Account(
            status=Account.Status.ACTIVE,
            account_type=AccountType.stripe,
            admin_id=user.id,
            country="US",
            currency="usd",
            is_details_submitted=True,
            is_charges_enabled=True,
            is_payouts_enabled=True,
            stripe_id="STRIPE_ACCOUNT_ID",
        )
        await save_fixture(account)

        await create_balance_transaction(save_fixture, account=account, amount=balance)

        # then
        session.expunge_all()

        with pytest.raises(InsufficientBalance):
            await payout_transaction_service.create_payout(
                session, locker, account=account
            )

    async def test_under_review_account(
        self, session: AsyncSession, locker: Locker, user: User
    ) -> None:
        account = Account(
            status=Account.Status.UNDER_REVIEW,
            account_type=AccountType.stripe,
            admin_id=user.id,
            country="US",
            currency="usd",
        )

        # then
        session.expunge_all()

        with pytest.raises(UnderReviewAccount):
            await payout_transaction_service.create_payout(
                session, locker, account=account
            )

    async def test_inactive_account(
        self, session: AsyncSession, locker: Locker, user: User
    ) -> None:
        account = Account(
            status=Account.Status.ONBOARDING_STARTED,
            account_type=AccountType.stripe,
            admin_id=user.id,
            country="US",
            currency="usd",
        )

        # then
        session.expunge_all()

        with pytest.raises(NotReadyAccount):
            await payout_transaction_service.create_payout(
                session, locker, account=account
            )

    async def test_payout_disabled_account(
        self, session: AsyncSession, locker: Locker, user: User
    ) -> None:
        account = Account(
            status=Account.Status.ACTIVE,
            account_type=AccountType.stripe,
            admin_id=user.id,
            country="US",
            currency="usd",
            is_payouts_enabled=False,
        )

        # then
        session.expunge_all()

        with pytest.raises(NotReadyAccount):
            await payout_transaction_service.create_payout(
                session, locker, account=account
            )

    async def test_stripe(
        self,
        session: AsyncSession,
        locker: Locker,
        save_fixture: SaveFixture,
        user: User,
        stripe_service_mock: MagicMock,
    ) -> None:
        account = Account(
            status=Account.Status.ACTIVE,
            account_type=AccountType.stripe,
            admin_id=user.id,
            country="US",
            currency="usd",
            is_details_submitted=True,
            is_charges_enabled=True,
            is_payouts_enabled=True,
            processor_fees_applicable=True,
            stripe_id="STRIPE_ACCOUNT_ID",
        )
        await save_fixture(account)

        payment_transaction_1 = await create_payment_transaction(save_fixture)
        balance_transaction_1 = await create_balance_transaction(
            save_fixture, account=account, payment_transaction=payment_transaction_1
        )

        payment_transaction_2 = await create_payment_transaction(save_fixture)
        balance_transaction_2 = await create_balance_transaction(
            save_fixture, account=account, payment_transaction=payment_transaction_2
        )

        stripe_service_mock.transfer.return_value = SimpleNamespace(
            id="STRIPE_TRANSFER_ID", balance_transaction="STRIPE_BALANCE_TRANSACTION_ID"
        )

        # then
        session.expunge_all()

        payout = await payout_transaction_service.create_payout(
            session, locker, account=account
        )

        assert payout.account == account
        assert payout.processor == Processor.stripe
        assert payout.payout_id is None
        assert payout.currency == "usd"
        assert payout.amount < 0
        assert payout.account_currency == "usd"
        assert payout.account_amount < 0

        assert len(payout.paid_transactions) == 2 + len(
            payout.account_incurred_transactions
        )
        assert payout.paid_transactions[0].id == balance_transaction_1.id
        assert payout.paid_transactions[1].id == balance_transaction_2.id

        assert len(payout.incurred_transactions) > 0
        assert (
            len(payout.account_incurred_transactions)
            == len(payout.incurred_transactions) / 2
        )

        transfer_mock: MagicMock = stripe_service_mock.transfer
        assert transfer_mock.call_count == 2
        for call in transfer_mock.call_args_list:
            assert call[0][0] == account.stripe_id
            assert call[1]["source_transaction"] in [
                payment_transaction_1.charge_id,
                payment_transaction_2.charge_id,
            ]
            # assert call[1]["transfer_group"] == str(payout.id)
            assert call[1]["metadata"]["payout_transaction_id"] == str(payout.id)

        stripe_service_mock.create_payout.assert_not_called()

    async def test_stripe_different_currencies(
        self,
        session: AsyncSession,
        locker: Locker,
        save_fixture: SaveFixture,
        user: User,
        stripe_service_mock: MagicMock,
    ) -> None:
        account = Account(
            status=Account.Status.ACTIVE,
            account_type=AccountType.stripe,
            admin_id=user.id,
            country="FR",
            currency="eur",
            is_details_submitted=True,
            is_charges_enabled=True,
            is_payouts_enabled=True,
            processor_fees_applicable=True,
            stripe_id="STRIPE_ACCOUNT_ID",
        )
        await save_fixture(account)

        payment_transaction_1 = await create_payment_transaction(save_fixture)
        balance_transaction_1 = await create_balance_transaction(
            save_fixture, account=account, payment_transaction=payment_transaction_1
        )

        payment_transaction_2 = await create_payment_transaction(save_fixture)
        balance_transaction_2 = await create_balance_transaction(
            save_fixture, account=account, payment_transaction=payment_transaction_2
        )

        stripe_service_mock.transfer.return_value = SimpleNamespace(
            id="STRIPE_TRANSFER_ID",
            balance_transaction="STRIPE_BALANCE_TRANSACTION_ID",
            destination_payment="STRIPE_DESTINATION_CHARGE_ID",
        )
        stripe_service_mock.get_charge.return_value = SimpleNamespace(
            id="STRIPE_DESTINATION_CHARGE_ID",
            balance_transaction=SimpleNamespace(
                amount=900, currency="eur", exchange_rate=0.9
            ),
        )
        stripe_service_mock.create_payout.return_value = SimpleNamespace(
            id="STRIPE_PAYOUT_ID"
        )

        # then
        session.expunge_all()

        payout = await payout_transaction_service.create_payout(
            session, locker, account=account
        )

        assert payout.account == account
        assert payout.processor == Processor.stripe
        assert payout.payout_id is None
        assert payout.currency == "usd"
        assert payout.amount < 0
        assert payout.account_currency == "eur"
        assert payout.account_amount < 0

        assert len(payout.paid_transactions) == 2 + len(
            payout.account_incurred_transactions
        )
        assert payout.paid_transactions[0].id == balance_transaction_1.id
        assert payout.paid_transactions[1].id == balance_transaction_2.id

        stripe_service_mock.create_payout.assert_not_called()

    async def test_stripe_refund(
        self,
        session: AsyncSession,
        locker: Locker,
        save_fixture: SaveFixture,
        user: User,
        stripe_service_mock: MagicMock,
    ) -> None:
        account = Account(
            status=Account.Status.ACTIVE,
            account_type=AccountType.stripe,
            admin_id=user.id,
            country="US",
            currency="usd",
            is_details_submitted=True,
            is_charges_enabled=True,
            is_payouts_enabled=True,
            processor_fees_applicable=True,
            stripe_id="STRIPE_ACCOUNT_ID",
        )
        await save_fixture(account)

        payment_transaction_1 = await create_payment_transaction(
            save_fixture, charge_id="CHARGE_ID_1"
        )
        balance_transaction_payment_1 = await create_balance_transaction(
            save_fixture, account=account, payment_transaction=payment_transaction_1
        )
        balance_transaction_fee_1 = await create_balance_transaction(
            save_fixture,
            account=account,
            amount=-100,
            balance_reversal_transaction=balance_transaction_payment_1,
        )

        payment_transaction_2 = await create_payment_transaction(
            save_fixture, charge_id="CHARGE_ID_2"
        )
        balance_transaction_payment_2 = await create_balance_transaction(
            save_fixture, account=account, payment_transaction=payment_transaction_2
        )
        balance_transaction_fee_2 = await create_balance_transaction(
            save_fixture,
            account=account,
            amount=-100,
            balance_reversal_transaction=balance_transaction_payment_2,
        )

        assert payment_transaction_2.charge_id is not None
        refund_transaction_2 = await create_refund_transaction(
            save_fixture,
            amount=-payment_transaction_2.amount,
            charge_id=payment_transaction_2.charge_id,
        )
        balance_transaction_refund_2 = await create_balance_transaction(
            save_fixture,
            account=account,
            amount=refund_transaction_2.amount,
            balance_reversal_transaction=balance_transaction_payment_2,
        )

        stripe_service_mock.transfer.return_value = SimpleNamespace(
            id="STRIPE_TRANSFER_ID", balance_transaction="STRIPE_BALANCE_TRANSACTION_ID"
        )

        payout = await payout_transaction_service.create_payout(
            session, locker, account=account
        )

        assert payout.account == account
        assert payout.processor == Processor.stripe
        assert payout.payout_id is None
        assert payout.currency == "usd"
        assert payout.amount < 0
        assert payout.account_currency == "usd"
        assert payout.account_amount < 0

        assert len(payout.paid_transactions) == 5 + len(
            payout.account_incurred_transactions
        )
        assert set(t.id for t in payout.paid_transactions).issuperset(
            {
                balance_transaction_payment_1.id,
                balance_transaction_fee_1.id,
                balance_transaction_payment_2.id,
                balance_transaction_fee_2.id,
                balance_transaction_refund_2.id,
            }
        )

        assert len(payout.incurred_transactions) > 0
        assert (
            len(payout.account_incurred_transactions)
            == len(payout.incurred_transactions) / 2
        )

        transfer_mock: MagicMock = stripe_service_mock.transfer
        assert transfer_mock.call_count == 1
        for call in transfer_mock.call_args_list:
            assert call[0][0] == account.stripe_id
            assert call[1]["source_transaction"] in [
                payment_transaction_1.charge_id,
                payment_transaction_2.charge_id,
            ]
            # assert call[1]["transfer_group"] == str(payout.id)
            assert call[1]["metadata"]["payout_transaction_id"] == str(payout.id)

        stripe_service_mock.create_payout.assert_not_called()

    async def test_stripe_refund_of_paid_payment(
        self,
        session: AsyncSession,
        locker: Locker,
        save_fixture: SaveFixture,
        user: User,
        stripe_service_mock: MagicMock,
    ) -> None:
        account = Account(
            status=Account.Status.ACTIVE,
            account_type=AccountType.stripe,
            admin_id=user.id,
            country="US",
            currency="usd",
            is_details_submitted=True,
            is_charges_enabled=True,
            is_payouts_enabled=True,
            processor_fees_applicable=True,
            stripe_id="STRIPE_ACCOUNT_ID",
        )
        await save_fixture(account)

        previous_payout = Transaction(
            type=TransactionType.payout,
            account=account,
            processor=Processor.stripe,
            currency="usd",
            amount=-10000,
            account_currency="usd",
            account_amount=-10000,
            tax_amount=0,
        )
        await save_fixture(previous_payout)

        payment_transaction_1 = await create_payment_transaction(
            save_fixture, charge_id="CHARGE_ID_1"
        )
        balance_transaction_1 = await create_balance_transaction(
            save_fixture,
            account=account,
            payment_transaction=payment_transaction_1,
            payout_transaction=previous_payout,
        )

        payment_transaction_2 = await create_payment_transaction(
            save_fixture, charge_id="CHARGE_ID_2"
        )
        balance_transaction_2 = await create_balance_transaction(
            save_fixture, account=account, payment_transaction=payment_transaction_2
        )

        payment_transaction_3 = await create_payment_transaction(
            save_fixture, charge_id="CHARGE_ID_3"
        )
        balance_transaction_3 = await create_balance_transaction(
            save_fixture, account=account, payment_transaction=payment_transaction_3
        )

        assert payment_transaction_1.charge_id is not None
        refund_transaction_1 = await create_refund_transaction(
            save_fixture,
            amount=-payment_transaction_1.amount,
            charge_id=payment_transaction_1.charge_id,
        )
        balance_transaction_4 = await create_balance_transaction(
            save_fixture,
            account=account,
            amount=refund_transaction_1.amount,
            balance_reversal_transaction=balance_transaction_1,
        )

        stripe_service_mock.transfer.return_value = SimpleNamespace(
            id="STRIPE_TRANSFER_ID", balance_transaction="STRIPE_BALANCE_TRANSACTION_ID"
        )

        # then
        session.expunge_all()

        payout = await payout_transaction_service.create_payout(
            session, locker, account=account
        )

        assert payout.account == account
        assert payout.processor == Processor.stripe
        assert payout.payout_id is None
        assert payout.currency == "usd"
        assert payout.amount < 0
        assert payout.account_currency == "usd"
        assert payout.account_amount < 0

        assert len(payout.paid_transactions) == 3 + len(
            payout.account_incurred_transactions
        )
        assert payout.paid_transactions[0].id == balance_transaction_2.id
        assert payout.paid_transactions[1].id == balance_transaction_3.id
        assert payout.paid_transactions[2].id == balance_transaction_4.id

        assert len(payout.incurred_transactions) > 0
        assert (
            len(payout.account_incurred_transactions)
            == len(payout.incurred_transactions) / 2
        )

        transfer_mock: MagicMock = stripe_service_mock.transfer
        assert transfer_mock.call_count == 1
        for call in transfer_mock.call_args_list:
            assert call[0][0] == account.stripe_id
            assert call[1]["source_transaction"] in [
                payment_transaction_2.charge_id,
                payment_transaction_3.charge_id,
            ]
            # assert call[1]["transfer_group"] == str(payout.id)
            assert call[1]["metadata"]["payout_transaction_id"] == str(payout.id)

        stripe_service_mock.create_payout.assert_not_called()

    async def test_open_collective(
        self,
        session: AsyncSession,
        locker: Locker,
        save_fixture: SaveFixture,
        user: User,
    ) -> None:
        account = Account(
            status=Account.Status.ACTIVE,
            account_type=AccountType.open_collective,
            admin_id=user.id,
            country="US",
            currency="usd",
            is_details_submitted=False,
            is_charges_enabled=False,
            is_payouts_enabled=False,
            processor_fees_applicable=True,
            open_collective_slug="polarsource",
        )
        await save_fixture(account)

        balance_transaction = await create_balance_transaction(
            save_fixture, account=account
        )

        # then
        session.expunge_all()

        payout = await payout_transaction_service.create_payout(
            session, locker, account=account
        )

        assert payout.account == account
        assert payout.processor == Processor.open_collective
        assert payout.currency == "usd"
        assert payout.amount == -balance_transaction.amount
        assert payout.account_currency == "usd"
        assert payout.account_amount == -balance_transaction.amount

        assert len(payout.paid_transactions) == 1 + len(
            payout.account_incurred_transactions
        )
        assert payout.paid_transactions[0].id == balance_transaction.id

        assert len(payout.incurred_transactions) == 0
        assert len(payout.account_incurred_transactions) == 0


def build_stripe_payout(
    *,
    status: str = "paid",
    currency: str = "usd",
    amount: int = 1000,
    balance_transaction: str | None = None,
) -> stripe_lib.Payout:
    return stripe_lib.Payout.construct_from(
        {
            "id": "STRIPE_PAYOUT_ID",
            "status": status,
            "currency": currency,
            "amount": amount,
            "balance_transaction": balance_transaction,
        },
        None,
    )


def build_stripe_balance_transaction(
    *, fee: int | None = 100, source_transfer: str | None = None
) -> stripe_lib.BalanceTransaction:
    return stripe_lib.BalanceTransaction.construct_from(
        {
            "id": "STRIPE_BALANCE_TRANSACTION_ID",
            "fee": fee,
            "source": (
                {"source_transfer": source_transfer}
                if source_transfer is not None
                else None
            ),
        },
        None,
    )


@pytest.mark.asyncio
class TestCreatePayoutFromStripe:
    async def test_not_paid_payout(self, session: AsyncSession) -> None:
        stripe_payout = build_stripe_payout(status="pending")

        # then
        session.expunge_all()

        with pytest.raises(StripePayoutNotPaid):
            await payout_transaction_service.create_payout_from_stripe(
                session, payout=stripe_payout, stripe_account_id="STRIPE_ACCOUNT_ID"
            )

    async def test_unknown_account(self, session: AsyncSession) -> None:
        stripe_payout = build_stripe_payout()

        # then
        session.expunge_all()

        with pytest.raises(UnknownAccount):
            await payout_transaction_service.create_payout_from_stripe(
                session, payout=stripe_payout, stripe_account_id="STRIPE_ACCOUNT_ID"
            )

    async def test_valid(
        self,
        session: AsyncSession,
        save_fixture: SaveFixture,
        user: User,
        stripe_service_mock: MagicMock,
    ) -> None:
        account = Account(
            status=Account.Status.ACTIVE,
            account_type=AccountType.stripe,
            admin_id=user.id,
            country="US",
            currency="usd",
            is_details_submitted=True,
            is_charges_enabled=True,
            is_payouts_enabled=True,
            processor_fees_applicable=True,
            stripe_id="STRIPE_ACCOUNT_ID",
        )
        await save_fixture(account)

        transaction_params = {
            "type": TransactionType.balance,
            "processor": Processor.stripe,
            "currency": "usd",
            "amount": 1000,
            "account_currency": "usd",
            "account_amount": 1000,
            "tax_amount": 0,
            "account": account,
        }
        transactions: list[Transaction] = []
        balance_transactions: list[stripe_lib.BalanceTransaction] = []
        for i in range(0, 3):
            transfer_id = f"STRIPE_TRANSFER_{i}"
            transaction = Transaction(**transaction_params, transfer_id=transfer_id)
            balance_transaction = build_stripe_balance_transaction(
                source_transfer=transfer_id
            )

            await save_fixture(transaction)
            transactions.append(transaction)
            balance_transactions.append(balance_transaction)

        stripe_service_mock.list_balance_transactions.return_value = (
            create_async_iterator(balance_transactions)
        )

        stripe_payout = build_stripe_payout(
            amount=sum(transaction.amount for transaction in transactions)
        )

        # then
        session.expunge_all()

        transaction = await payout_transaction_service.create_payout_from_stripe(
            session, payout=stripe_payout, stripe_account_id="STRIPE_ACCOUNT_ID"
        )

        assert transaction.type == TransactionType.payout
        assert transaction.processor == Processor.stripe
        assert transaction.currency == stripe_payout.currency
        assert transaction.amount == -stripe_payout.amount
        assert transaction.tax_amount == 0
        assert transaction.payout_id == stripe_payout.id
        assert transaction.account == account

        paid_transactions_statement = select(Transaction).where(
            Transaction.payout_transaction_id == transaction.id
        )
        result = await session.execute(paid_transactions_statement)
        paid_transactions = result.scalars().all()
        assert len(paid_transactions) == len(transactions)

    async def test_valid_different_currencies(
        self,
        session: AsyncSession,
        save_fixture: SaveFixture,
        user: User,
        stripe_service_mock: MagicMock,
    ) -> None:
        account = Account(
            status=Account.Status.ACTIVE,
            account_type=AccountType.stripe,
            admin_id=user.id,
            country="FR",
            currency="eur",
            is_details_submitted=True,
            is_charges_enabled=True,
            is_payouts_enabled=True,
            processor_fees_applicable=True,
            stripe_id="STRIPE_ACCOUNT_ID",
        )
        await save_fixture(account)

        transaction_params = {
            "type": TransactionType.balance,
            "processor": Processor.stripe,
            "currency": "usd",
            "amount": 1000,
            "account_currency": "eur",
            "account_amount": 900,
            "tax_amount": 0,
            "account": account,
        }
        transactions: list[Transaction] = []
        balance_transactions: list[stripe_lib.BalanceTransaction] = []
        for i in range(0, 3):
            transfer_id = f"STRIPE_TRANSFER_{i}"
            transaction = Transaction(**transaction_params, transfer_id=transfer_id)
            balance_transaction = build_stripe_balance_transaction(
                source_transfer=transfer_id
            )

            await save_fixture(transaction)
            transactions.append(transaction)
            balance_transactions.append(balance_transaction)

        stripe_service_mock.list_balance_transactions.return_value = (
            create_async_iterator(balance_transactions)
        )

        stripe_payout = build_stripe_payout(
            amount=sum(transaction.account_amount for transaction in transactions),
            currency="eur",
        )

        # then
        session.expunge_all()

        transaction = await payout_transaction_service.create_payout_from_stripe(
            session, payout=stripe_payout, stripe_account_id="STRIPE_ACCOUNT_ID"
        )

        assert transaction.type == TransactionType.payout
        assert transaction.processor == Processor.stripe
        assert transaction.currency == "usd"
        assert transaction.amount == -sum(
            transaction.amount for transaction in transactions
        )
        assert transaction.account_currency == "eur"
        assert transaction.account_amount == -stripe_payout.amount
        assert transaction.tax_amount == 0
        assert transaction.payout_id == stripe_payout.id
        assert transaction.account == account

        paid_transactions_statement = select(Transaction).where(
            Transaction.payout_transaction_id == transaction.id
        )
        result = await session.execute(paid_transactions_statement)
        paid_transactions = result.scalars().all()
        assert len(paid_transactions) == len(transactions)


@pytest.mark.asyncio
class TestTriggerStripePayouts:
    async def test_valid(
        self,
        mocker: MockerFixture,
        session: AsyncSession,
        save_fixture: SaveFixture,
        organization: Organization,
        user: User,
        organization_second: Organization,
        user_second: User,
    ) -> None:
        enqueue_job_mock = mocker.patch("polar.transaction.service.payout.enqueue_job")

        account_1 = await create_account(save_fixture, organization, user)
        account_2 = await create_account(save_fixture, organization_second, user_second)

        payout_1 = await create_transaction(
            save_fixture,
            account=account_1,
            type=TransactionType.payout,
            created_at=utc_now() - datetime.timedelta(days=14),
        )
        payout_2 = await create_transaction(
            save_fixture,
            account=account_1,
            type=TransactionType.payout,
            created_at=utc_now() - datetime.timedelta(days=7),
        )
        payout_3 = await create_transaction(
            save_fixture,
            account=account_2,
            type=TransactionType.payout,
            created_at=utc_now() - datetime.timedelta(days=7),
        )

        await payout_transaction_service.trigger_stripe_payouts(session)

        assert enqueue_job_mock.call_count == 2
        enqueue_job_mock.assert_any_call(
            "payout.trigger_stripe_payout", payout_id=payout_1.id
        )
        enqueue_job_mock.assert_any_call(
            "payout.trigger_stripe_payout", payout_id=payout_3.id
        )
