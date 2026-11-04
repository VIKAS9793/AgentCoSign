package com.agentcosign.core.domain

import com.agentcosign.core.model.Limits
import com.agentcosign.core.model.Money
import java.time.Duration
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class LimitsRuleTest {

    private val valid = Limits(
        automaticUpTo = Money.usd("25"),
        approvalUpTo = Money.usd("250"),
        dailyMost = Money.usd("1000"),
        perCustomerCount = 3,
        perCustomerWindow = Duration.ofHours(24),
        timeToDecide = Duration.ofHours(4),
    )

    @Test fun defaultsFromTheDesignAreValid() = assertNull(LimitsRule.check(valid))

    @Test fun equalLimitsAreAllowed() =
        assertNull(LimitsRule.check(valid.copy(automaticUpTo = Money.usd("250"), dailyMost = Money.usd("250"))))

    @Test fun zeroIsRefused() =
        assertEquals(LimitsRule.Violation.NotPositive, LimitsRule.check(valid.copy(automaticUpTo = Money.ZERO)))

    @Test fun automaticAboveApprovalIsRefused() = assertEquals(
        LimitsRule.Violation.AutomaticAboveApproval,
        LimitsRule.check(valid.copy(automaticUpTo = Money.usd("250.01"))),
    )

    @Test fun approvalAboveDailyIsRefused() = assertEquals(
        LimitsRule.Violation.ApprovalAboveDaily,
        LimitsRule.check(valid.copy(approvalUpTo = Money.usd("1000.01"))),
    )

    @Test fun decisionWindowIsBounded() {
        assertEquals(
            LimitsRule.Violation.BadDecisionTime,
            LimitsRule.check(valid.copy(timeToDecide = Duration.ofMinutes(4))),
        )
        assertEquals(
            LimitsRule.Violation.BadDecisionTime,
            LimitsRule.check(valid.copy(timeToDecide = Duration.ofHours(25))),
        )
    }

    @Test fun customerWindowNeedsCountAndTime() = assertEquals(
        LimitsRule.Violation.BadCustomerWindow,
        LimitsRule.check(valid.copy(perCustomerCount = 0)),
    )
}
