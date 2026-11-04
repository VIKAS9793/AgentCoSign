package com.agentcosign.core.data

import com.agentcosign.core.model.Decision
import com.agentcosign.core.model.DecisionResult
import com.agentcosign.core.model.Money
import com.agentcosign.core.model.Outcome
import java.time.Clock
import java.time.Duration
import java.time.Instant
import java.time.ZoneId
import java.time.ZoneOffset
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class FakeApprovalsRepositoryTest {

    private val start = Instant.parse("2026-11-04T09:00:00Z")
    private fun repo(at: Instant = start) = FakeApprovalsRepository(Clock.fixed(at, ZoneOffset.UTC))

    @Test fun seedMatchesTheDesign() = runTest {
        val home = repo().home.first()
        assertEquals(1, home.waiting.size)
        assertEquals(Money.usd("98.00"), home.refundedTotal)
        assertEquals(2, home.declinedOrStopped)
    }

    @Test fun approvingMovesTheRequestToTheLedger() = runTest {
        val repo = repo()
        val result = repo.decide("req-arjun", Decision.APPROVE)
        assertTrue(result is DecisionResult.Done)
        val home = repo.home.first()
        assertTrue(home.waiting.isEmpty())
        assertEquals(Money.usd("138.00"), home.refundedTotal)
    }

    @Test fun rejectingRecordsADecline() = runTest {
        val repo = repo()
        val result = repo.decide("req-arjun", Decision.REJECT) as DecisionResult.Done
        assertEquals(Outcome.DECLINED, result.entry.outcome)
    }

    @Test fun stoppedRefundsRefuseApproval() = runTest {
        val repo = repo()
        repo.setRefundsStopped(true)
        assertTrue(repo.decide("req-arjun", Decision.APPROVE) is DecisionResult.Refused)
        assertEquals(1, repo.home.first().waiting.size)
    }

    @Test fun stoppedRefundsStillAllowReject() = runTest {
        val repo = repo()
        repo.setRefundsStopped(true)
        assertTrue(repo.decide("req-arjun", Decision.REJECT) is DecisionResult.Done)
    }

    @Test fun expiredRequestIsRefused() = runTest {
        val clock = MovableClock(start)
        val repo = FakeApprovalsRepository(clock)
        clock.now = start.plus(Duration.ofHours(5))
        val result = repo.decide("req-arjun", Decision.APPROVE) as DecisionResult.Refused
        assertEquals("The time to decide has passed.", result.reason)
    }

    @Test fun decidingTwiceIsRefused() = runTest {
        val repo = repo()
        repo.decide("req-arjun", Decision.APPROVE)
        assertTrue(repo.decide("req-arjun", Decision.APPROVE) is DecisionResult.Refused)
    }
}

private class MovableClock(var now: Instant) : Clock() {
    override fun getZone(): ZoneId = ZoneOffset.UTC
    override fun withZone(zone: ZoneId): Clock = this
    override fun instant(): Instant = now
}
