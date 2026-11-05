package com.agentcosign.feature.settings

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.hilt.lifecycle.viewmodel.compose.hiltViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.agentcosign.core.designsystem.BrandChip
import com.agentcosign.core.designsystem.CoSign
import com.agentcosign.core.designsystem.GroupCard
import com.agentcosign.core.designsystem.GroupDivider
import com.agentcosign.core.designsystem.NotBuiltNote
import com.agentcosign.core.designsystem.RowTrailing
import com.agentcosign.core.designsystem.SectionHeader
import com.agentcosign.core.designsystem.SettingRow
import com.agentcosign.core.designsystem.StatusPill
import com.agentcosign.core.designsystem.ToggleRow
import com.agentcosign.core.model.Alerts
import com.agentcosign.core.model.Settings
import java.time.Duration

@Composable
fun SettingsRoute(onBack: () -> Unit, viewModel: SettingsViewModel = hiltViewModel()) {
    val state by viewModel.state.collectAsStateWithLifecycle()
    SettingsScreen(state, onBack, viewModel::setAlerts, viewModel::setAssistantEnabled)
}

@Composable
internal fun SettingsScreen(
    state: SettingsUiState,
    onBack: () -> Unit,
    onAlerts: (Alerts) -> Unit,
    onAssistant: (Boolean) -> Unit,
) {
    Column(Modifier.fillMaxSize().background(CoSign.colors.ground).safeDrawingPadding().padding(horizontal = 20.dp, vertical = 12.dp)) {
        BrandChip(sampleData = true)
        Row(verticalAlignment = Alignment.CenterVertically) {
            IconButton(onClick = onBack) { Icon(Icons.AutoMirrored.Filled.ArrowBack, "Back") }
            Text("Settings", style = MaterialTheme.typography.headlineMedium)
        }
        when (state) {
            SettingsUiState.Loading -> Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) { CircularProgressIndicator() }
            is SettingsUiState.Error -> Text(state.message, color = CoSign.colors.danger)
            is SettingsUiState.Success -> SettingsContent(state.settings, onAlerts, onAssistant)
        }
    }
}

@Composable
private fun SettingsContent(settings: Settings, onAlerts: (Alerts) -> Unit, onAssistant: (Boolean) -> Unit) {
    val colors = CoSign.colors
    val limits = settings.limits
    Column(Modifier.verticalScroll(rememberScrollState())) {
        SectionHeader("Your limits")
        GroupCard {
            SettingRow("Refunds on their own up to", value = limits.automaticUpTo.display())
            GroupDivider()
            SettingRow("Needs your approval up to", subtitle = "Above this, refunds are refused.", value = limits.approvalUpTo.display())
            GroupDivider()
            SettingRow("Most per day", value = limits.dailyMost.display())
            GroupDivider()
            SettingRow("Refunds per customer", value = "${limits.perCustomerCount} in ${hours(limits.perCustomerWindow)}")
            GroupDivider()
            SettingRow("Time to decide", value = hours(limits.timeToDecide))
        }
        Hint("Set on your AgentCoSign server.")
        NotBuiltNote("Changing limits from the phone isn't available yet.")

        SectionHeader("Who can suggest refunds")
        GroupCard {
            settings.suggesters.forEachIndexed { index, person ->
                if (index > 0) GroupDivider()
                if (person.isAssistant) {
                    ToggleRow(person.name, person.enabled, onAssistant)
                } else {
                    SettingRow(person.name, subtitle = person.role)
                }
            }
            GroupDivider()
            SettingRow("Add a person", trailing = RowTrailing.CHEVRON)
        }
        Hint("Only you can approve.")
        NotBuiltNote("Staff accounts aren't built yet.")

        SectionHeader("Phones that can approve")
        GroupCard {
            settings.devices.forEachIndexed { index, device ->
                if (index > 0) GroupDivider()
                SettingRow(device.label) { if (device.active) StatusPill("Active", colors.good, colors.goodSoft) }
            }
            GroupDivider()
            SettingRow("Add another phone", trailing = RowTrailing.CHEVRON)
        }

        SectionHeader("Alerts")
        GroupCard {
            val alerts = settings.alerts
            ToggleRow("Refund waiting for you", alerts.refundWaiting, { onAlerts(alerts.copy(refundWaiting = it)) })
            GroupDivider()
            ToggleRow("A message looked like a trick", alerts.trickMessage, { onAlerts(alerts.copy(trickMessage = it)) })
            GroupDivider()
            ToggleRow("Daily summary", alerts.dailySummary, { onAlerts(alerts.copy(dailySummary = it)) })
        }
        NotBuiltNote("Push alerts aren't built yet.")

        SectionHeader("PayPal account")
        GroupCard {
            SettingRow("Business account ending ${settings.paypal.accountEnding}") {
                if (settings.paypal.connected) StatusPill("Connected", colors.good, colors.goodSoft)
            }
            GroupDivider()
            SettingRow("What we can see", trailing = RowTrailing.CHEVRON)
            GroupDivider()
            SettingRow("Disconnect", subtitle = "Refunds through this app stop until you connect again.", titleColor = colors.danger)
        }

        SectionHeader("Your record")
        GroupCard {
            SettingRow("Download refund history", subtitle = "Signed, so any change can be detected.", trailing = RowTrailing.CHEVRON)
        }

        SectionHeader("Always on")
        GroupCard {
            SettingRow("Refunds never pass what the customer paid", trailing = RowTrailing.LOCK)
            GroupDivider()
            SettingRow("Open disputes always need you", trailing = RowTrailing.LOCK)
            GroupDivider()
            SettingRow("Messages that look like tricks are refused", trailing = RowTrailing.LOCK)
            GroupDivider()
            SettingRow("If a check can't run, nothing goes out on its own", trailing = RowTrailing.LOCK)
        }
        Hint("These protect you and can't be turned off.")
        Spacer(Modifier.height(32.dp))
    }
}

@Composable
private fun Hint(text: String) =
    Text(text, style = MaterialTheme.typography.bodyMedium, color = CoSign.colors.muted, modifier = Modifier.padding(top = 8.dp, start = 2.dp))

private fun hours(duration: Duration): String = when {
    duration.toMinutes() % 60 != 0L -> "${duration.toMinutes()} minutes"
    duration.toHours() == 1L -> "1 hour"
    else -> "${duration.toHours()} hours"
}
