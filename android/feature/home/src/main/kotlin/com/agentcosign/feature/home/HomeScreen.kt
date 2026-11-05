package com.agentcosign.feature.home

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.selection.selectable
import androidx.compose.ui.semantics.Role
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowForward
import androidx.compose.material.icons.outlined.CheckCircle
import androidx.compose.material.icons.outlined.ChatBubbleOutline
import androidx.compose.material.icons.outlined.HighlightOff
import androidx.compose.material.icons.outlined.History
import androidx.compose.material.icons.outlined.Pause
import androidx.compose.material.icons.outlined.PlayArrow
import androidx.compose.material.icons.outlined.Schedule
import androidx.compose.material.icons.outlined.Settings
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.hilt.lifecycle.viewmodel.compose.hiltViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.agentcosign.core.designsystem.BrandChip
import com.agentcosign.core.designsystem.CoSign
import com.agentcosign.core.designsystem.GroupCard
import com.agentcosign.core.designsystem.GroupDivider
import com.agentcosign.core.designsystem.SecondaryButton
import com.agentcosign.core.designsystem.StatTile
import com.agentcosign.core.designsystem.StatusPill
import com.agentcosign.core.model.HomeSnapshot
import com.agentcosign.core.model.LedgerEntry
import com.agentcosign.core.model.Outcome
import com.agentcosign.core.model.PendingRequest
import java.time.ZoneId
import java.time.format.DateTimeFormatter

@Composable
fun HomeRoute(onReview: (String) -> Unit, onSettings: () -> Unit, viewModel: HomeViewModel = hiltViewModel()) {
    val state by viewModel.state.collectAsStateWithLifecycle()
    HomeScreen(state, onReview, onSettings, viewModel::setRefundsStopped)
}

private enum class HomeTab { REQUESTS, ACTIVITY }

@Composable
internal fun HomeScreen(state: HomeUiState, onReview: (String) -> Unit, onSettings: () -> Unit, onStopChange: (Boolean) -> Unit) {
    Box(Modifier.fillMaxSize().background(CoSign.colors.ground).safeDrawingPadding()) {
        when (state) {
            HomeUiState.Loading -> CircularProgressIndicator(Modifier.align(Alignment.Center))
            is HomeUiState.Error -> Text(state.message, Modifier.align(Alignment.Center).padding(24.dp), color = CoSign.colors.danger)
            is HomeUiState.Success -> HomeContent(state.home, onReview, onSettings, onStopChange)
        }
    }
}

@Composable
private fun HomeContent(home: HomeSnapshot, onReview: (String) -> Unit, onSettings: () -> Unit, onStopChange: (Boolean) -> Unit) {
    var tab by rememberSaveable { mutableStateOf(HomeTab.REQUESTS) }
    var confirmStop by remember { mutableStateOf(false) }
    val colors = CoSign.colors

    LazyColumn(Modifier.fillMaxSize().padding(horizontal = 20.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
        item(key = "brand") { BrandChip(Modifier.padding(top = 12.dp), sampleData = true) }
        item(key = "header") {
            Row(Modifier.fillMaxWidth().padding(top = 12.dp), verticalAlignment = Alignment.Top) {
                Column(Modifier.weight(1f)) {
                    Text("PAYPAL BUSINESS ACCOUNT", style = MaterialTheme.typography.labelMedium, color = colors.muted)
                    Text("Hello, ${home.ownerName}", style = MaterialTheme.typography.headlineMedium)
                }
                Surface(Modifier.size(40.dp), shape = CircleShape, color = colors.ink) {
                    Box(contentAlignment = Alignment.Center) { Text(home.ownerName.take(1), color = colors.surface, fontWeight = FontWeight.Bold) }
                }
                IconButton(onClick = onSettings) { Icon(Icons.Outlined.Settings, "Settings") }
            }
        }
        if (home.refundsStopped) {
            item(key = "stopped") {
                StatusPill("All refunds are stopped", colors.danger, colors.dangerSoft, icon = Icons.Outlined.Pause)
            }
        }
        item(key = "period") { Text("Last 24 hours", style = MaterialTheme.typography.labelLarge, modifier = Modifier.padding(top = 8.dp)) }
        item(key = "tiles1") {
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                StatTile("Waiting", home.waiting.size.toString(), colors.waiting, Modifier.weight(1f), Icons.Outlined.Schedule)
                StatTile("Refunded", home.refundedTotal.display(), colors.good, Modifier.weight(1f), Icons.Outlined.CheckCircle)
            }
        }
        item(key = "tiles2") {
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                StatTile("Declined or stopped", home.declinedOrStopped.toString(), colors.danger, Modifier.weight(1f), Icons.Outlined.HighlightOff)
                StatTile("Daily limit used", home.refundedTotal.display(), colors.muted, Modifier.weight(1f)) {
                    Text("of ${home.dailyLimit.display()}", style = MaterialTheme.typography.bodyMedium, color = colors.muted)
                    LinearProgressIndicator(
                        progress = { home.dailyUsedFraction },
                        modifier = Modifier.fillMaxWidth(),
                        color = colors.ink,
                        trackColor = colors.border,
                    )
                }
            }
        }
        item(key = "tabs") { Tabs(tab, home.waiting.size) { tab = it } }
        when (tab) {
            HomeTab.REQUESTS -> {
                item(key = "requests-title") { Text("Waiting for your approval", style = MaterialTheme.typography.labelLarge) }
                if (home.waiting.isEmpty()) {
                    item(key = "requests-empty") { Text("Nothing is waiting for you.", color = colors.muted) }
                }
                items(home.waiting, key = { it.id }) { RequestCard(it) { onReview(it.id) } }
            }
            HomeTab.ACTIVITY -> {
                item(key = "ledger-title") {
                    Row {
                        Text("Today's refund ledger", style = MaterialTheme.typography.labelLarge, modifier = Modifier.weight(1f))
                        Text("${home.refundedTotal.display()} sent", style = MaterialTheme.typography.labelLarge)
                    }
                }
                item(key = "ledger") {
                    GroupCard {
                        home.ledger.forEachIndexed { index, entry ->
                            if (index > 0) GroupDivider()
                            LedgerRow(entry)
                        }
                    }
                }
            }
        }
        item(key = "stop") {
            Column(Modifier.padding(top = 12.dp, bottom = 24.dp)) {
                if (home.refundsStopped) {
                    SecondaryButton("Resume refunds", { onStopChange(false) }, icon = Icons.Outlined.PlayArrow)
                } else {
                    SecondaryButton("Stop all refunds", { confirmStop = true }, icon = Icons.Outlined.Pause)
                }
                Text(
                    "Stops new refunds and approvals at once. Refunds already with PayPal will finish.",
                    style = MaterialTheme.typography.bodyMedium,
                    color = colors.muted,
                    modifier = Modifier.padding(top = 8.dp),
                )
            }
        }
    }

    if (confirmStop) {
        AlertDialog(
            onDismissRequest = { confirmStop = false },
            title = { Text("Stop all refunds?") },
            text = { Text("Nothing new goes out until you resume. Refunds already with PayPal will finish.") },
            confirmButton = { TextButton({ confirmStop = false; onStopChange(true) }) { Text("Stop all refunds") } },
            dismissButton = { TextButton({ confirmStop = false }) { Text("Cancel") } },
        )
    }
}

@Composable
private fun Tabs(selected: HomeTab, waiting: Int, onSelect: (HomeTab) -> Unit) {
    Surface(shape = RoundedCornerShape(14.dp), color = CoSign.colors.border.copy(alpha = 0.5f)) {
        Row(Modifier.padding(4.dp)) {
            TabButton("Requests", Icons.Outlined.ChatBubbleOutline, selected == HomeTab.REQUESTS, waiting, Modifier.weight(1f)) { onSelect(HomeTab.REQUESTS) }
            TabButton("Activity", Icons.Outlined.History, selected == HomeTab.ACTIVITY, null, Modifier.weight(1f)) { onSelect(HomeTab.ACTIVITY) }
        }
    }
}

@Composable
private fun TabButton(label: String, icon: ImageVector, selected: Boolean, badge: Int?, modifier: Modifier, onClick: () -> Unit) {
    val colors = CoSign.colors
    val fg = if (selected) colors.surface else colors.ink
    Surface(
        modifier
            .heightIn(min = 48.dp)
            .selectable(selected = selected, role = Role.Tab, onClick = onClick),
        shape = RoundedCornerShape(10.dp),
        color = if (selected) colors.ink else colors.ground.copy(alpha = 0f),
    ) {
        Row(Modifier.padding(vertical = 14.dp), horizontalArrangement = Arrangement.Center, verticalAlignment = Alignment.CenterVertically) {
            Icon(icon, null, tint = fg, modifier = Modifier.size(18.dp))
            Spacer(Modifier.size(8.dp))
            Text(label, color = fg, style = MaterialTheme.typography.labelLarge)
            if (badge != null && badge > 0) {
                Spacer(Modifier.size(8.dp))
                Surface(shape = CircleShape, color = if (selected) colors.surface else colors.ground) {
                    Text(badge.toString(), color = colors.ink, modifier = Modifier.padding(horizontal = 7.dp), style = MaterialTheme.typography.labelLarge)
                }
            }
        }
    }
}

private val TIME: DateTimeFormatter = DateTimeFormatter.ofPattern("HH:mm")

@Composable
private fun RequestCard(request: PendingRequest, onClick: () -> Unit) {
    val colors = CoSign.colors
    Surface(
        Modifier
            .fillMaxWidth()
            .clickable(onClickLabel = "Review", role = Role.Button, onClick = onClick),
        shape = RoundedCornerShape(14.dp),
        color = colors.surface,
        border = BorderStroke(1.dp, colors.border),
    ) {
        Row(Modifier.padding(14.dp), verticalAlignment = Alignment.CenterVertically) {
            Surface(Modifier.size(36.dp), shape = CircleShape, color = colors.waitingSoft) {
                Icon(Icons.Outlined.Schedule, null, tint = colors.waiting, modifier = Modifier.padding(8.dp))
            }
            Column(Modifier.weight(1f).padding(horizontal = 12.dp)) {
                Text(request.customer, style = MaterialTheme.typography.titleMedium)
                Text("Decide by ${TIME.format(request.decideBy.atZone(ZoneId.systemDefault()))}", color = colors.muted, style = MaterialTheme.typography.bodyMedium)
            }
            Column(horizontalAlignment = Alignment.End) {
                Text(request.amount.display(), style = MaterialTheme.typography.titleMedium)
                Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.padding(top = 4.dp)) {
                    Text("Review", style = MaterialTheme.typography.labelLarge)
                    Icon(Icons.AutoMirrored.Filled.ArrowForward, null, Modifier.size(14.dp))
                }
            }
        }
    }
}

@Composable
private fun LedgerRow(entry: LedgerEntry) {
    val colors = CoSign.colors
    Row(Modifier.fillMaxWidth().padding(14.dp), verticalAlignment = Alignment.CenterVertically) {
        Column(Modifier.weight(1f)) {
            Text(entry.customer, style = MaterialTheme.typography.titleMedium)
            Text(entry.amount.display(), color = colors.muted, style = MaterialTheme.typography.bodyMedium)
        }
        when (entry.outcome) {
            Outcome.REFUNDED -> StatusPill("Refunded", colors.good, colors.goodSoft)
            Outcome.DECLINED -> StatusPill("Declined", colors.danger, colors.dangerSoft, icon = Icons.Outlined.HighlightOff)
            Outcome.STOPPED -> StatusPill("Stopped", colors.danger, colors.dangerSoft, icon = Icons.Outlined.Pause)
            Outcome.EXPIRED -> StatusPill("Expired", colors.muted, colors.surface, icon = Icons.Outlined.Schedule)
        }
    }
}
