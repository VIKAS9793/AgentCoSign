package com.agentcosign.feature.review

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.outlined.Fingerprint
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
import com.agentcosign.core.designsystem.MoneyText
import com.agentcosign.core.designsystem.NotBuiltNote
import com.agentcosign.core.designsystem.PrimaryButton
import com.agentcosign.core.designsystem.SecondaryButton
import com.agentcosign.core.designsystem.SettingRow
import com.agentcosign.core.model.Decision
import com.agentcosign.core.model.Outcome
import com.agentcosign.core.model.PendingRequest

@Composable
fun ReviewRoute(requestId: String, onDone: () -> Unit) {
    val viewModel = hiltViewModel<ReviewViewModel, ReviewViewModel.Factory>(key = requestId) { it.create(requestId) }
    val state by viewModel.state.collectAsStateWithLifecycle()
    ReviewScreen(state, viewModel::decide, onDone)
}

@Composable
internal fun ReviewScreen(state: ReviewUiState, onDecide: (Decision) -> Unit, onDone: () -> Unit) {
    Column(Modifier.fillMaxSize().background(CoSign.colors.ground).safeDrawingPadding().padding(horizontal = 20.dp, vertical = 12.dp)) {
        BrandChip(sampleData = true)
        IconButton(onClick = onDone) { Icon(Icons.AutoMirrored.Filled.ArrowBack, "Back") }
        when (state) {
            ReviewUiState.Loading -> Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) { CircularProgressIndicator() }
            ReviewUiState.Empty -> Text("This request is no longer waiting.", style = MaterialTheme.typography.titleMedium)
            is ReviewUiState.Decided -> Decided(state, onDone)
            is ReviewUiState.Ready -> Ready(state, onDecide)
        }
    }
}

@Composable
private fun Ready(state: ReviewUiState.Ready, onDecide: (Decision) -> Unit) {
    val request: PendingRequest = state.request
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState())) {
        Text("Refund to ${request.customer}", style = MaterialTheme.typography.headlineMedium)
        MoneyText(request.amount.display(), Modifier.padding(vertical = 12.dp), style = MaterialTheme.typography.headlineLarge)
        GroupCard {
            SettingRow("Customer paid", value = request.paid.display())
            GroupDivider()
            SettingRow("Already refunded", value = request.alreadyRefunded.display())
            GroupDivider()
            SettingRow("Reason", subtitle = request.reason)
            GroupDivider()
            SettingRow("Suggested by", value = request.proposedBy)
        }
        if (state.refusal != null) {
            Text(state.refusal, color = CoSign.colors.danger, modifier = Modifier.padding(top = 12.dp))
        }
        Spacer(Modifier.height(20.dp))
        NotBuiltNote("Fingerprint check isn't connected yet. Sample data only.")
        Row(Modifier.padding(top = 12.dp), horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            PrimaryButton("Approve", { onDecide(Decision.APPROVE) }, enabled = !state.deciding, icon = Icons.Outlined.Fingerprint)
            SecondaryButton("Decline", { onDecide(Decision.REJECT) })
        }
    }
}

@Composable
private fun Decided(state: ReviewUiState.Decided, onDone: () -> Unit) {
    val entry = state.result.entry
    val text = if (entry.outcome == Outcome.REFUNDED) "${entry.amount.display()} refunded to ${entry.customer}" else "Declined. Nothing was sent."
    Column(Modifier.fillMaxWidth()) {
        Text(text, style = MaterialTheme.typography.headlineMedium)
        Spacer(Modifier.height(20.dp))
        PrimaryButton("Done", onDone)
    }
}
