package com.agentcosign.feature.onboarding

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.outlined.Fingerprint
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import com.agentcosign.core.designsystem.CoSign
import com.agentcosign.core.designsystem.NotBuiltNote
import com.agentcosign.core.designsystem.PrimaryButton
import com.agentcosign.core.designsystem.SecondaryButton

/**
 * The gateway checks passkeys against one web origin, so a passkey made by this
 * app would be refused. Until the gateway accepts the app's origin, "Set up" only
 * moves the flow forward and no credential is created.
 */
@Composable
fun EnrolPhoneScreen(onSetUp: () -> Unit, onNotNow: () -> Unit, onBack: () -> Unit) {
    OnboardingScaffold(
        title = "Set up this phone to approve refunds",
        onBack = onBack,
        bottom = {
            NotBuiltNote("Passkey setup isn't connected yet. No passkey is created.")
            PrimaryButton("Set up this phone", onSetUp, icon = Icons.Outlined.Fingerprint, modifier = Modifier.padding(top = 12.dp))
            SecondaryButton("Not now", onNotNow, modifier = Modifier.padding(top = 8.dp))
            Text(
                "Until this is set up, refunds over your limit can't be approved.",
                style = MaterialTheme.typography.bodyMedium,
                color = CoSign.colors.muted,
                modifier = Modifier.padding(top = 6.dp),
            )
        },
    ) {
        Box(Modifier.fillMaxWidth().padding(vertical = 24.dp), contentAlignment = Alignment.Center) {
            Surface(Modifier.size(200.dp), shape = CircleShape, color = CoSign.colors.ground, border = BorderStroke(1.dp, CoSign.colors.border)) {}
            Surface(shape = RoundedCornerShape(24.dp), color = CoSign.colors.surface, shadowElevation = 4.dp) {
                Icon(Icons.Outlined.Fingerprint, null, Modifier.padding(24.dp).size(48.dp))
            }
        }
        Column(Modifier.fillMaxWidth(), horizontalAlignment = Alignment.CenterHorizontally) {
            Text("A quick check that it is you", style = MaterialTheme.typography.titleMedium)
            Text(
                "Your fingerprint stays on this phone. AgentCoSign only receives confirmation that the check passed.",
                style = MaterialTheme.typography.bodyMedium,
                color = CoSign.colors.muted,
                textAlign = TextAlign.Center,
                modifier = Modifier.padding(top = 8.dp),
            )
            Text("Use your fingerprint, face or PIN.", fontWeight = FontWeight.SemiBold, modifier = Modifier.padding(top = 8.dp))
        }
    }
}
