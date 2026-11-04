package com.agentcosign.core.designsystem

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Typography
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.Immutable
import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.sp

/** Colours taken from the approved mockups: cream ground, green-black ink, rust accent. */
@Immutable
data class CoSignColors(
    val ground: Color = Color(0xFFF3F0E9),
    val surface: Color = Color(0xFFFBFAF6),
    val border: Color = Color(0xFFE4E0D6),
    val ink: Color = Color(0xFF1F2A24),
    val muted: Color = Color(0xFF5C625D),
    val accent: Color = Color(0xFFC4673F),
    val good: Color = Color(0xFF1E6B3A),
    val goodSoft: Color = Color(0xFFE3F0E5),
    val waiting: Color = Color(0xFF8A5A00),
    val waitingSoft: Color = Color(0xFFF6E8C3),
    val danger: Color = Color(0xFFA8241B),
)

val LocalCoSignColors = staticCompositionLocalOf { CoSignColors() }

object CoSign {
    val colors: CoSignColors
        @Composable get() = LocalCoSignColors.current
}

private val CoSignTypography = Typography(
    headlineLarge = TextStyle(fontSize = 30.sp, lineHeight = 34.sp, fontWeight = FontWeight.Bold, letterSpacing = (-0.6).sp),
    headlineMedium = TextStyle(fontSize = 26.sp, lineHeight = 30.sp, fontWeight = FontWeight.Bold, letterSpacing = (-0.5).sp),
    titleMedium = TextStyle(fontSize = 16.sp, lineHeight = 22.sp, fontWeight = FontWeight.SemiBold),
    bodyLarge = TextStyle(fontSize = 16.sp, lineHeight = 22.sp),
    bodyMedium = TextStyle(fontSize = 14.sp, lineHeight = 20.sp),
    labelLarge = TextStyle(fontSize = 15.sp, lineHeight = 20.sp, fontWeight = FontWeight.SemiBold),
    labelMedium = TextStyle(fontSize = 13.sp, lineHeight = 16.sp, fontWeight = FontWeight.SemiBold, letterSpacing = 1.2.sp),
)

@Composable
fun CoSignTheme(content: @Composable () -> Unit) {
    val colors = CoSignColors()
    val scheme = lightColorScheme(
        primary = colors.ink,
        onPrimary = colors.surface,
        secondary = colors.accent,
        background = colors.ground,
        onBackground = colors.ink,
        surface = colors.surface,
        onSurface = colors.ink,
        onSurfaceVariant = colors.muted,
        outline = colors.border,
        error = colors.danger,
    )
    androidx.compose.runtime.CompositionLocalProvider(LocalCoSignColors provides colors) {
        MaterialTheme(colorScheme = scheme, typography = CoSignTypography, content = content)
    }
}
