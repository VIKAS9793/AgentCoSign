package com.agentcosign.core.data

import com.agentcosign.core.domain.SettingsRepository
import com.agentcosign.core.model.Alerts
import com.agentcosign.core.model.ApproverDevice
import com.agentcosign.core.model.Limits
import com.agentcosign.core.model.Money
import com.agentcosign.core.model.PayPalLink
import com.agentcosign.core.model.Settings
import com.agentcosign.core.model.Suggester
import java.time.Duration
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update

/** In-memory settings matching the gateway defaults in `config.yaml`. */
class FakeSettingsRepository : SettingsRepository {

    private val state = MutableStateFlow(
        Settings(
            limits = Limits(
                automaticUpTo = Money.usd("25.00"),
                approvalUpTo = Money.usd("250.00"),
                dailyMost = Money.usd("1000.00"),
                perCustomerCount = 3,
                perCustomerWindow = Duration.ofHours(24),
                timeToDecide = Duration.ofHours(4),
            ),
            suggesters = listOf(
                Suggester("AI assistant", "Can suggest", isAssistant = true, enabled = true),
                Suggester("Priya", "Staff. Can suggest, can't approve", isAssistant = false, enabled = true),
            ),
            devices = listOf(ApproverDevice("This phone", active = true)),
            alerts = Alerts(refundWaiting = true, trickMessage = true, dailySummary = false),
            paypal = PayPalLink(accountEnding = "TGL2", connected = true),
        ),
    )

    override val settings: Flow<Settings> = state.asStateFlow()

    override suspend fun setAlerts(alerts: Alerts) = state.update { it.copy(alerts = alerts) }

    override suspend fun setAssistantEnabled(enabled: Boolean) = state.update { current ->
        current.copy(suggesters = current.suggesters.map { if (it.isAssistant) it.copy(enabled = enabled) else it })
    }
}
