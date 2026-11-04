package com.agentcosign.core.domain

import com.agentcosign.core.model.Alerts
import com.agentcosign.core.model.Decision
import com.agentcosign.core.model.DecisionResult
import com.agentcosign.core.model.HomeSnapshot
import com.agentcosign.core.model.Limits
import com.agentcosign.core.model.PendingRequest
import com.agentcosign.core.model.Settings
import kotlinx.coroutines.flow.Flow

/** The single source of truth for requests and today's ledger. */
interface ApprovalsRepository {
    val home: Flow<HomeSnapshot>

    suspend fun refresh()

    suspend fun request(id: String): PendingRequest?

    /** Decide one request. The real gateway demands a passkey signature bound to [id] and [decision]. */
    suspend fun decide(id: String, decision: Decision): DecisionResult

    /** Stop or resume every new refund and approval at once. */
    suspend fun setRefundsStopped(stopped: Boolean)
}

interface SettingsRepository {
    val settings: Flow<Settings>

    /** Replace the limits. Fails with [LimitsRule.Violation] when they do not hold together. */
    suspend fun saveLimits(limits: Limits): Result<Unit>

    suspend fun setAlerts(alerts: Alerts)

    suspend fun setAssistantEnabled(enabled: Boolean)
}
