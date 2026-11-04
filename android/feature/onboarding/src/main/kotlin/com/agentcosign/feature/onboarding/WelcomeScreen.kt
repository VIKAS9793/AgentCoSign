package com.agentcosign.feature.onboarding

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.offset
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.outlined.Shield
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.rotate
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.agentcosign.core.designsystem.BrandChip
import com.agentcosign.core.designsystem.CoSign
import com.agentcosign.core.designsystem.PrimaryButton

@Composable
fun WelcomeScreen(onGetStarted: () -> Unit) {
    Column(Modifier.fillMaxSize().background(CoSign.colors.ground).safeDrawingPadding().padding(20.dp)) {
        BrandChip(sampleData = true)
        Box(Modifier.fillMaxWidth().weight(1f), contentAlignment = Alignment.Center) {
            Surface(Modifier.size(240.dp), shape = CircleShape, color = CoSign.colors.ground, border = BorderStroke(1.dp, CoSign.colors.border)) {}
            Surface(
                Modifier.rotate(-3f),
                shape = RoundedCornerShape(14.dp),
                color = CoSign.colors.surface,
                border = BorderStroke(1.dp, CoSign.colors.border),
                shadowElevation = 6.dp,
            ) {
                Column(Modifier.padding(horizontal = 20.dp, vertical = 16.dp)) {
                    Text("Refund request", color = CoSign.colors.muted, style = MaterialTheme.typography.bodyMedium)
                    Text("$40.00", style = MaterialTheme.typography.headlineMedium, fontWeight = FontWeight.Bold, modifier = Modifier.padding(vertical = 6.dp))
                    Text("to Arjun Mehta", color = CoSign.colors.muted, style = MaterialTheme.typography.bodyMedium)
                }
            }
            Icon(
                Icons.Outlined.Shield,
                contentDescription = null,
                tint = CoSign.colors.surface,
                modifier = Modifier.offset(x = 84.dp, y = 56.dp).background(CoSign.colors.ink, RoundedCornerShape(10.dp)).padding(10.dp),
            )
        }
        Text("MONEY OUT, WITH YOUR SAY", style = MaterialTheme.typography.labelMedium, color = CoSign.colors.muted)
        Text(
            "Approve money leaving your PayPal account, from your phone",
            style = MaterialTheme.typography.headlineLarge,
            modifier = Modifier.padding(top = 8.dp),
        )
        Text(
            "Review every important refund and confirm it is really you before money moves.",
            style = MaterialTheme.typography.bodyLarge,
            color = CoSign.colors.muted,
            modifier = Modifier.padding(top = 12.dp),
        )
        Spacer(Modifier.height(20.dp))
        PrimaryButton("Get started", onGetStarted)
    }
}
