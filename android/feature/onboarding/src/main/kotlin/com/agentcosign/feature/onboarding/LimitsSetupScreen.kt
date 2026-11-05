package com.agentcosign.feature.onboarding

import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.outlined.Schedule
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.hilt.lifecycle.viewmodel.compose.hiltViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.agentcosign.core.designsystem.CoSign
import com.agentcosign.core.designsystem.GroupCard
import com.agentcosign.core.designsystem.GroupDivider
import com.agentcosign.core.designsystem.MoneyText
import com.agentcosign.core.designsystem.NotBuiltNote
import com.agentcosign.core.designsystem.PrimaryButton
import com.agentcosign.core.designsystem.SettingRow

/**
 * Shows what can move on its own and what waits for the owner.
 *
 * @param onContinue moves to phone setup.
 * @param onBack returns to the previous step.
 */
@Composable
fun LimitsSetupRoute(onContinue: () -> Unit, onBack: () -> Unit, viewModel: LimitsViewModel = hiltViewModel()) {
    val state by viewModel.state.collectAsStateWithLifecycle()
    LimitsSetupScreen(state, onContinue, onBack)
}

@Composable
internal fun LimitsSetupScreen(state: LimitsUiState, onContinue: () -> Unit, onBack: () -> Unit) {
    OnboardingScaffold(
        title = "What can move automatically",
        subtitle = "Small refunds can happen automatically. Larger ones wait for you.",
        onBack = onBack,
        bottom = { PrimaryButton("Continue", onContinue, enabled = state is LimitsUiState.Success) },
    ) {
        when (state) {
            LimitsUiState.Loading -> CircularProgressIndicator()
            is LimitsUiState.Error -> Text(state.message, color = CoSign.colors.danger)
            is LimitsUiState.Success -> {
                val limits = state.limits
                GroupCard {
                    SettingRow("Automatic limit", subtitle = "Per refund") { MoneyText(limits.automaticUpTo.display()) }
                    GroupDivider()
                    SettingRow("Most per day", subtitle = "Past this, refunds are refused until the total drops.") {
                        MoneyText(limits.dailyMost.display())
                    }
                }
                Spacer(Modifier.height(12.dp))
                Row(Modifier.fillMaxWidth().padding(4.dp), verticalAlignment = Alignment.Top) {
                    Icon(Icons.Outlined.Schedule, null, Modifier.size(16.dp), tint = CoSign.colors.muted)
                    Spacer(Modifier.width(8.dp))
                    Text(
                        "Refunds over ${limits.automaticUpTo.display()} wait for your approval before anything leaves your account.",
                        style = MaterialTheme.typography.bodyMedium,
                        color = CoSign.colors.muted,
                    )
                }
                NotBuiltNote("These are set on your AgentCoSign server. Changing them from the phone isn't available yet.")
            }
        }
    }
}
