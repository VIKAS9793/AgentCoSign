package com.agentcosign.feature.onboarding

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.outlined.CheckCircle
import androidx.compose.material.icons.outlined.LinkOff
import androidx.compose.material.icons.outlined.Lock
import androidx.compose.material.icons.outlined.Shield
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.unit.dp
import com.agentcosign.core.designsystem.CoSign
import com.agentcosign.core.designsystem.NotBuiltNote
import com.agentcosign.core.designsystem.PrimaryButton
import com.agentcosign.core.designsystem.SecondaryButton

/** PayPal partner onboarding is not built; "Connect" only moves the flow forward. */
@Composable
fun ConnectPayPalScreen(onConnect: () -> Unit, onNotNow: () -> Unit, onBack: () -> Unit) {
    OnboardingScaffold(
        title = "Connect your PayPal business account?",
        subtitle = "PayPal will open securely so you can choose the business account you want to protect.",
        onBack = onBack,
        bottom = {
            NotBuiltNote("PayPal sign-in isn't built yet. This step uses the demo account.")
            Row(Modifier.padding(top = 12.dp), horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                PrimaryButton("Connect PayPal account", onConnect)
                SecondaryButton("Not now", onNotNow)
            }
        },
    ) {
        Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Point(Icons.Outlined.Shield, "Reads your orders, refunds and disputes", "To check every request against PayPal's records.")
            Point(Icons.Outlined.CheckCircle, "Your limits decide what moves", "Small refunds go out on their own. Bigger ones wait for your fingerprint.")
            Point(Icons.Outlined.LinkOff, "Disconnect whenever you want", "Manage the connection in Settings.")
        }
        Row(Modifier.padding(top = 16.dp), verticalAlignment = Alignment.CenterVertically) {
            Icon(Icons.Outlined.Lock, null, Modifier.size(16.dp), tint = CoSign.colors.muted)
            Spacer(Modifier.width(8.dp))
            Text("Never sees your PayPal password.", color = CoSign.colors.muted, style = MaterialTheme.typography.bodyMedium)
        }
    }
}

@Composable
private fun Point(icon: ImageVector, title: String, body: String) {
    Surface(Modifier.fillMaxWidth(), shape = RoundedCornerShape(14.dp), color = CoSign.colors.surface, border = BorderStroke(1.dp, CoSign.colors.border)) {
        Row(Modifier.padding(14.dp)) {
            Icon(icon, null, Modifier.padding(end = 12.dp).size(22.dp))
            Column {
                Text(title, style = MaterialTheme.typography.titleMedium)
                Text(body, style = MaterialTheme.typography.bodyMedium, color = CoSign.colors.muted)
            }
        }
    }
}
