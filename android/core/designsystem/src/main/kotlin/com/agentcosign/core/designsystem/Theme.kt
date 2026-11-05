package com.agentcosign.core.designsystem

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Typography
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.Immutable
import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.sp

/**
 * Colour roles for the app. Light values come from the approved mockups (cream ground,
 * green-black ink, rust accent); dark values keep the same roles at accessible contrast.
 */
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

/** Dark counterparts of the light roles. Status is never shown by colour alone. */
val DarkCoSignColors = CoSignColors(
    ground = Color(0xFF151A17),
    surface = Color(0xFF1E2420),
    border = Color(0xFF323A35),
    ink = Color(0xFFECEAE3),
    muted = Color(0xFFA7ADA8),
    accent = Color(0xFFE08A63),
    good = Color(0xFF7FCB98),
    goodSoft = Color(0xFF1F3A2A),
    waiting = Color(0xFFE5B85C),
    waitingSoft = Color(0xFF3A2F17),
    danger = Color(0xFFF08A80),
)

val LocalCoSignColors = staticCompositionLocalOf { CoSignColors() }

/**
 * Tabular figures, so amounts line up in lists and do not shift width when they change.
 * Apply to every text that shows money.
 */
const val TABULAR_FIGURES = "tnum"

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

/**
 * App theme. Follows the system light or dark setting.
 *
 * @param darkTheme whether to use the dark roles; defaults to the system setting.
 */
@Composable
fun CoSignTheme(darkTheme: Boolean = isSystemInDarkTheme(), content: @Composable () -> Unit) {
    val colors = if (darkTheme) DarkCoSignColors else CoSignColors()
    val base = if (darkTheme) darkColorScheme() else lightColorScheme()
    val scheme = base.copy(
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
