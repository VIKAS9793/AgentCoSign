package com.agentcosign.core.domain

import com.agentcosign.core.model.Limits
import com.agentcosign.core.model.Money
import java.time.Duration

/** Limits must nest: automatic <= needs approval <= most per day, all above zero. */
object LimitsRule {

    sealed class Violation(message: String) : IllegalArgumentException(message) {
        data object NotPositive : Violation("Every limit must be above zero.")
        data object AutomaticAboveApproval : Violation("The automatic limit can't be above the approval limit.")
        data object ApprovalAboveDaily : Violation("The approval limit can't be above the daily most.")
        data object BadCustomerWindow : Violation("Refunds per customer needs a count and a time window.")
        data object BadDecisionTime : Violation("Time to decide must be between 5 minutes and 24 hours.")
    }

    private val MIN_DECIDE: Duration = Duration.ofMinutes(5)
    private val MAX_DECIDE: Duration = Duration.ofHours(24)

    fun check(limits: Limits): Violation? = with(limits) {
        when {
            listOf(automaticUpTo, approvalUpTo, dailyMost).any { it <= Money.ZERO } -> Violation.NotPositive
            automaticUpTo > approvalUpTo -> Violation.AutomaticAboveApproval
            approvalUpTo > dailyMost -> Violation.ApprovalAboveDaily
            perCustomerCount < 1 || perCustomerWindow <= Duration.ZERO -> Violation.BadCustomerWindow
            timeToDecide < MIN_DECIDE || timeToDecide > MAX_DECIDE -> Violation.BadDecisionTime
            else -> null
        }
    }
}
