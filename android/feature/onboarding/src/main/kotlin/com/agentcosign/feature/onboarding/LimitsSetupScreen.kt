package com.agentcosign.feature.onboarding

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.outlined.Schedule
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import androidx.hilt.navigation.compose.hiltViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.agentcosign.core.designsystem.CoSign
import com.agentcosign.core.designsystem.PrimaryButton

@Composable
fun LimitsSetupRoute(onSaved: () -> Unit, onBack: () -> Unit, viewModel: LimitsSetupViewModel = hiltViewModel()) {
    val state by viewModel.state.collectAsStateWithLifecycle()
    LaunchedEffect(viewModel) { viewModel.saved.collect { onSaved() } }
    LimitsSetupScreen(state, viewModel::onAutomaticChange, viewModel::onDailyChange, viewModel::save, onBack)
}

@Composable
internal fun LimitsSetupScreen(
    state: LimitsSetupUiState,
    onAutomaticChange: (String) -> Unit,
    onDailyChange: (String) -> Unit,
    onSave: () -> Unit,
    onBack: () -> Unit,
) {
    OnboardingScaffold(
        title = "Choose what can move automatically",
        subtitle = "Small refunds can happen automatically. Larger ones wait for you.",
        onBack = onBack,
        bottom = { PrimaryButton("Save limits", onSave, enabled = !state.saving) },
    ) {
        AmountCard("Automatic limit", "Per refund", state.automatic, onAutomaticChange)
        Spacer(Modifier.height(12.dp))
        AmountCard("Most per day", "Past this, refunds are refused until the total drops.", state.daily, onDailyChange)
        Spacer(Modifier.height(12.dp))
        Row(Modifier.fillMaxWidth().padding(4.dp), verticalAlignment = Alignment.Top) {
            Icon(Icons.Outlined.Schedule, null, Modifier.size(16.dp), tint = CoSign.colors.muted)
            Spacer(Modifier.width(8.dp))
            Text(
                "Refunds over $${state.automatic.ifBlank { "0" }} wait for your approval before anything leaves your account.",
                style = MaterialTheme.typography.bodyMedium,
                color = CoSign.colors.muted,
            )
        }
        if (state.error != null) {
            Text(state.error, color = CoSign.colors.danger, style = MaterialTheme.typography.bodyMedium, modifier = Modifier.padding(top = 8.dp))
        }
    }
}

@Composable
private fun AmountCard(title: String, subtitle: String, value: String, onChange: (String) -> Unit) {
    Surface(Modifier.fillMaxWidth(), shape = RoundedCornerShape(14.dp), color = CoSign.colors.surface, border = BorderStroke(1.dp, CoSign.colors.border)) {
        Row(Modifier.padding(14.dp), verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f).padding(end = 12.dp)) {
                Text(title, style = MaterialTheme.typography.titleMedium)
                Text(subtitle, style = MaterialTheme.typography.bodyMedium, color = CoSign.colors.muted)
            }
            OutlinedTextField(
                value = value,
                onValueChange = onChange,
                prefix = { Text("$") },
                singleLine = true,
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Decimal),
                modifier = Modifier.width(140.dp),
                textStyle = MaterialTheme.typography.titleMedium,
            )
        }
    }
}
