package com.agentcosign.core.model

import java.time.Instant

/** A refund the assistant or staff proposed, waiting for the owner. */
data class PendingRequest(
    val id: String,
    val customer: String,
    val amount: Money,
    val paid: Money,
    val alreadyRefunded: Money,
    val reason: String,
    val proposedBy: String,
    val decideBy: Instant,
)

enum class Outcome { REFUNDED, DECLINED, STOPPED, EXPIRED }

/** A request that has finished, for today's ledger. */
data class LedgerEntry(
    val id: String,
    val customer: String,
    val amount: Money,
    val outcome: Outcome,
    val at: Instant,
)

/** Everything the home screen shows for the last 24 hours. */
data class HomeSnapshot(
    val accountEnding: String,
    val ownerName: String,
    val waiting: List<PendingRequest>,
    val ledger: List<LedgerEntry>,
    val dailyLimit: Money,
    val refundsStopped: Boolean,
) {
    val refundedTotal: Money
        get() = ledger.filter { it.outcome == Outcome.REFUNDED }
            .fold(Money.ZERO) { sum, entry -> sum + entry.amount }

    /** Share of the daily limit already refunded, from 0 to 1, for a progress bar. */
    val dailyUsedFraction: Float
        get() = if (dailyLimit.amount.signum() <= 0) 1f
        else refundedTotal.amount.divide(dailyLimit.amount, 4, java.math.RoundingMode.HALF_UP).toFloat().coerceIn(0f, 1f)

    val declinedOrStopped: Int
        get() = ledger.count { it.outcome != Outcome.REFUNDED }
}

/** The owner's decision on one request. */
enum class Decision { APPROVE, REJECT }

/** What the gateway reports after a decision. */
sealed interface DecisionResult {
    data class Done(val entry: LedgerEntry) : DecisionResult
    data class Refused(val reason: String) : DecisionResult
}
