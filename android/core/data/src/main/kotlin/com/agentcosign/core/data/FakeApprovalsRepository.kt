package com.agentcosign.core.data

import com.agentcosign.core.domain.ApprovalsRepository
import com.agentcosign.core.model.Decision
import com.agentcosign.core.model.DecisionResult
import com.agentcosign.core.model.HomeSnapshot
import com.agentcosign.core.model.LedgerEntry
import com.agentcosign.core.model.Money
import com.agentcosign.core.model.Outcome
import com.agentcosign.core.model.PendingRequest
import com.agentcosign.core.model.SeenFigures
import java.time.Clock
import java.time.Duration
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock

/**
 * In-memory stand-in for the gateway, seeded with the design's sample day.
 *
 * It applies the same refusals the gateway applies so the screens can show them,
 * but it is not the policy engine and must never be shipped as one.
 */
class FakeApprovalsRepository(private val clock: Clock) : ApprovalsRepository {

    private val lock = Mutex()
    private val state = MutableStateFlow(seed())

    override val home: Flow<HomeSnapshot> = state.asStateFlow()

    override suspend fun refresh() = Unit

    override suspend fun request(id: String): PendingRequest? = state.value.waiting.firstOrNull { it.id == id }

    override suspend fun decide(id: String, decision: Decision, seen: SeenFigures): DecisionResult = lock.withLock {
        val now = state.value
        val pending = now.waiting.firstOrNull { it.id == id }
            ?: return DecisionResult.Refused("This request is no longer waiting.")
        val refusal = when {
            seen.paid != pending.paid || seen.alreadyRefunded != pending.alreadyRefunded ->
                "This order changed since you opened it. Check the new figures."
            decision == Decision.REJECT -> null
            now.refundsStopped -> "Refunds are stopped. Resume them first."
            clock.instant().isAfter(pending.decideBy) -> "The time to decide has passed."
            pending.amount > pending.paid - pending.alreadyRefunded -> "This is more than the customer paid."
            now.refundedTotal + pending.amount > now.dailyLimit -> "This would pass the most per day."
            else -> null
        }
        if (refusal != null) return DecisionResult.Refused(refusal)
        val outcome = if (decision == Decision.APPROVE) Outcome.REFUNDED else Outcome.DECLINED
        val entry = LedgerEntry(pending.id, pending.customer, pending.amount, outcome, clock.instant())
        state.update { it.copy(waiting = it.waiting - pending, ledger = listOf(entry) + it.ledger) }
        DecisionResult.Done(entry)
    }

    override suspend fun setRefundsStopped(stopped: Boolean) {
        lock.withLock { state.update { it.copy(refundsStopped = stopped) } }
    }

    private fun seed(): HomeSnapshot {
        val now = clock.instant()
        return HomeSnapshot(
            accountEnding = "TGL2",
            ownerName = "Vikas",
            waiting = listOf(
                PendingRequest(
                    id = "req-arjun",
                    customer = "Arjun Mehta",
                    amount = Money.usd("40.00"),
                    paid = Money.usd("40.00"),
                    alreadyRefunded = Money.ZERO,
                    reason = "Item arrived damaged",
                    proposedBy = "AI assistant",
                    decideBy = now.plus(Duration.ofHours(4)),
                ),
            ),
            ledger = listOf(
                LedgerEntry("req-lina", "Lina Ortiz", Money.usd("58.00"), Outcome.REFUNDED, now.minus(Duration.ofHours(2))),
                LedgerEntry("req-sameera", "Sameera Khan", Money.usd("40.00"), Outcome.REFUNDED, now.minus(Duration.ofHours(3))),
                LedgerEntry("req-omar", "Omar Haddad", Money.usd("310.00"), Outcome.DECLINED, now.minus(Duration.ofHours(5))),
                LedgerEntry("req-mei", "Mei Tanaka", Money.usd("75.00"), Outcome.STOPPED, now.minus(Duration.ofHours(6))),
            ),
            dailyLimit = Money.usd("1000.00"),
            refundsStopped = false,
        )
    }
}
