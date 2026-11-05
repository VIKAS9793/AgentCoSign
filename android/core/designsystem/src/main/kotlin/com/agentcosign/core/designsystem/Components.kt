package com.agentcosign.core.designsystem

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.selection.toggleable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.KeyboardArrowRight
import androidx.compose.material.icons.outlined.CheckCircle
import androidx.compose.material.icons.outlined.Lock
import androidx.compose.material.icons.outlined.Shield
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Surface
import androidx.compose.material3.Switch
import androidx.compose.material3.SwitchDefaults
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp

private val CardShape = RoundedCornerShape(14.dp)

@Composable
fun BrandChip(modifier: Modifier = Modifier, sampleData: Boolean = false) {
    Row(modifier, verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        Surface(shape = RoundedCornerShape(12.dp), color = CoSign.colors.surface, border = BorderStroke(1.dp, CoSign.colors.border)) {
            Row(Modifier.padding(horizontal = 8.dp, vertical = 6.dp), verticalAlignment = Alignment.CenterVertically) {
                Icon(
                    Icons.Outlined.Shield,
                    contentDescription = null,
                    tint = Color.White,
                    modifier = Modifier.clip(RoundedCornerShape(6.dp)).background(CoSign.colors.accent).padding(3.dp).size(16.dp),
                )
                Spacer(Modifier.width(8.dp))
                Text("AgentCoSign", style = MaterialTheme.typography.labelLarge)
            }
        }
        if (sampleData) StatusPill("Sample data", CoSign.colors.waiting, CoSign.colors.waitingSoft)
    }
}

@Composable
fun SectionHeader(text: String, modifier: Modifier = Modifier) {
    Text(
        text.uppercase(),
        style = MaterialTheme.typography.labelMedium,
        color = CoSign.colors.muted,
        modifier = modifier.padding(top = 24.dp, bottom = 8.dp),
    )
}

/** A rounded card whose children are separated by hairlines, as in Settings. */
@Composable
fun GroupCard(modifier: Modifier = Modifier, content: @Composable ColumnScope.() -> Unit) {
    Surface(modifier.fillMaxWidth(), shape = CardShape, color = CoSign.colors.surface, border = BorderStroke(1.dp, CoSign.colors.border)) {
        Column(content = content)
    }
}

@Composable
fun GroupDivider() = HorizontalDivider(color = CoSign.colors.border)

enum class RowTrailing { NONE, CHEVRON, LOCK }

@Composable
fun SettingRow(
    title: String,
    modifier: Modifier = Modifier,
    subtitle: String? = null,
    value: String? = null,
    titleColor: Color = Color.Unspecified,
    trailing: RowTrailing = RowTrailing.NONE,
    onClick: (() -> Unit)? = null,
    end: (@Composable () -> Unit)? = null,
) {
    val clickable = if (onClick != null) Modifier.clickable(role = Role.Button, onClick = onClick) else Modifier
    Row(
        modifier.fillMaxWidth().heightIn(min = 56.dp).then(clickable).padding(horizontal = 14.dp, vertical = 12.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Column(Modifier.weight(1f)) {
            Text(title, style = MaterialTheme.typography.titleMedium.copy(fontSize = MaterialTheme.typography.bodyMedium.fontSize), color = titleColor)
            if (subtitle != null) Text(subtitle, style = MaterialTheme.typography.bodyMedium, color = CoSign.colors.muted)
        }
        if (value != null) MoneyText(value, Modifier.padding(start = 8.dp))
        end?.invoke()
        when (trailing) {
            RowTrailing.CHEVRON -> Icon(Icons.AutoMirrored.Filled.KeyboardArrowRight, null, Modifier.padding(start = 6.dp))
            RowTrailing.LOCK -> Icon(Icons.Outlined.Lock, "Always on", Modifier.padding(start = 6.dp).size(18.dp))
            RowTrailing.NONE -> Unit
        }
    }
}

/**
 * A row with an on/off switch. The whole row is one control for TalkBack (role Switch),
 * so the label and its state are read together and the switch is not a second stop.
 */
@Composable
fun ToggleRow(title: String, checked: Boolean, onChange: (Boolean) -> Unit, modifier: Modifier = Modifier) {
    SettingRow(
        title,
        modifier.toggleable(value = checked, role = Role.Switch, onValueChange = onChange),
    ) {
        Switch(
            checked = checked,
            onCheckedChange = null,
            colors = SwitchDefaults.colors(
                checkedTrackColor = CoSign.colors.ink,
                uncheckedBorderColor = CoSign.colors.controlBorder,
                uncheckedThumbColor = CoSign.colors.controlBorder,
            ),
        )
    }
}

@Composable
fun StatusPill(text: String, color: Color, background: Color, modifier: Modifier = Modifier, icon: ImageVector? = Icons.Outlined.CheckCircle) {
    Row(
        modifier.clip(RoundedCornerShape(50)).background(background).padding(horizontal = 10.dp, vertical = 4.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        if (icon != null) {
            Icon(icon, null, tint = color, modifier = Modifier.size(14.dp))
            Spacer(Modifier.width(4.dp))
        }
        Text(text, color = color, style = MaterialTheme.typography.labelLarge.copy(fontSize = MaterialTheme.typography.bodyMedium.fontSize))
    }
}

@Composable
fun StatTile(label: String, value: String, labelColor: Color, modifier: Modifier = Modifier, icon: ImageVector? = null, footer: (@Composable () -> Unit)? = null) {
    Surface(modifier, shape = CardShape, color = CoSign.colors.surface, border = BorderStroke(1.dp, CoSign.colors.border)) {
        Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                if (icon != null) {
                    Icon(icon, null, tint = labelColor, modifier = Modifier.size(16.dp))
                    Spacer(Modifier.width(6.dp))
                }
                Text(label, color = labelColor, style = MaterialTheme.typography.labelLarge)
            }
            Text(
                value,
                style = MaterialTheme.typography.headlineMedium.copy(
                    fontSize = MaterialTheme.typography.titleMedium.fontSize * 1.4f,
                    fontFeatureSettings = TABULAR_FIGURES,
                ),
                fontWeight = FontWeight.Bold,
            )
            footer?.invoke()
        }
    }
}

@Composable
fun PrimaryButton(text: String, onClick: () -> Unit, modifier: Modifier = Modifier, enabled: Boolean = true, icon: ImageVector? = null) {
    Button(
        onClick = onClick,
        enabled = enabled,
        modifier = modifier.heightIn(min = 48.dp),
        shape = RoundedCornerShape(10.dp),
        colors = ButtonDefaults.buttonColors(containerColor = CoSign.colors.ink, contentColor = CoSign.colors.surface),
    ) {
        if (icon != null) {
            Icon(icon, null, Modifier.size(18.dp))
            Spacer(Modifier.width(8.dp))
        }
        Text(text, style = MaterialTheme.typography.labelLarge)
    }
}

@Composable
fun SecondaryButton(text: String, onClick: () -> Unit, modifier: Modifier = Modifier, icon: ImageVector? = null) {
    OutlinedButton(
        onClick = onClick,
        modifier = modifier.heightIn(min = 48.dp),
        shape = RoundedCornerShape(10.dp),
        border = BorderStroke(1.dp, CoSign.colors.controlBorder),
        colors = ButtonDefaults.outlinedButtonColors(containerColor = CoSign.colors.surface, contentColor = CoSign.colors.ink),
    ) {
        if (icon != null) {
            Icon(icon, null, Modifier.size(18.dp))
            Spacer(Modifier.width(8.dp))
        }
        Text(text, style = MaterialTheme.typography.labelLarge)
    }
}

/** Marks a control whose gateway endpoint does not exist yet, so nobody mistakes it for working. Waiting colour on the page meets AA (5.2:1). */
@Composable
fun NotBuiltNote(text: String = "Not connected yet. Changes stay on this phone.", modifier: Modifier = Modifier) {
    Text(text, style = MaterialTheme.typography.bodyMedium, color = CoSign.colors.waiting, modifier = modifier.padding(top = 6.dp, start = 2.dp))
}

/**
 * Text for an amount or other figure, set with tabular figures so columns line up.
 * The value is shown as given; this never animates or reformats digits.
 */
@Composable
fun MoneyText(text: String, modifier: Modifier = Modifier, style: androidx.compose.ui.text.TextStyle = MaterialTheme.typography.labelLarge) {
    Text(text, modifier = modifier, style = style.copy(fontFeatureSettings = TABULAR_FIGURES))
}
