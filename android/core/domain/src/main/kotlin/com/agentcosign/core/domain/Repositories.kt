package com.agentcosign.core.domain

import com.agentcosign.core.model.Alerts
import com.agentcosign.core.model.Decision
import com.agentcosign.core.model.DecisionResult
import com.agentcosign.core.model.HomeSnapshot
import com.agentcosign.core.model.PendingRequest
import com.agentcosign.core.model.SeenFigures
import com.agentcosign.core.model.Settings
import kotlinx.coroutines.flow.Flow

/** The single source of truth for requests and today's ledger. */
interface ApprovalsRepository {
    val home: Flow<HomeSnapshot>

    suspend fun refresh()

    suspend fun request(id: String): PendingRequest?

    /**
     * Decide one request.
     *
     * The real gateway demands a passkey signature bound to [id] and [decision], and
     * refuses when [seen] no longer matches what PayPal reports.
     *
     * @param seen the paid and already-refunded figures exactly as shown to the owner.
     */
    suspend fun decide(id: String, decision: Decision, seen: SeenFigures): DecisionResult

    /** Stop or resume every new refund and approval at once. */
    suspend fun setRefundsStopped(stopped: Boolean)
}

/**
 * The owner's settings as the gateway reports them.
 *
 * Limits are read-only: they live in the gateway's configuration and no endpoint
 * changes them, so the phone never edits or validates them.
 */
interface SettingsRepository {
    val settings: Flow<Settings>

    suspend fun setAlerts(alerts: Alerts)

    suspend fun setAssistantEnabled(enabled: Boolean)
}
