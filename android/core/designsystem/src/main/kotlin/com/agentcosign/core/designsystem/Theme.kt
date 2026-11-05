package com.agentcosign.core.designsystem

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Typography
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.Immutable
import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.sp

/**
 * Colour tokens taken from the approved mockups. The app has one light theme only.
 *
 * Contrast is measured, not estimated (WCAG 2.x relative luminance):
 * - Text roles meet AA (4.5:1) on both [surface] and [ground]: ink 14.2/13.0,
 *   muted 6.0/5.5, good 6.3, waiting 5.7/5.2, danger 6.9/6.3.
 * - Status text on its own soft background meets AA: good on [goodSoft] 5.6,
 *   waiting on [waitingSoft] 4.9, danger on [dangerSoft] 5.8.
 * - [controlBorder] meets the 3:1 non-text minimum for interactive boundaries
 *   (3.4 on surface, 3.1 on ground). [border] is for decorative card edges only and
 *   must never be the only thing that shows where a control is.
 * - White on [accent] is 3.9:1: use the accent for icons and brand marks, never for text.
 *
 * Status is never carried by colour alone: every status pairs a colour with an icon and words.
 */
@Immutable
data class CoSignColors(
    val ground: Color = Color(0xFFF3F0E9),
    val surface: Color = Color(0xFFFBFAF6),
    val border: Color = Color(0xFFE4E0D6),
    val controlBorder: Color = Color(0xFF858A84),
    val ink: Color = Color(0xFF1F2A24),
    val muted: Color = Color(0xFF5C625D),
    val accent: Color = Color(0xFFC4673F),
    val good: Color = Color(0xFF1E6B3A),
    val goodSoft: Color = Color(0xFFE3F0E5),
    val waiting: Color = Color(0xFF8A5A00),
    val waitingSoft: Color = Color(0xFFF6E8C3),
    val danger: Color = Color(0xFFA8241B),
    val dangerSoft: Color = Color(0xFFF7E4E1),
)

val LocalCoSignColors = staticCompositionLocalOf { CoSignColors() }

/** Access to the app's colour tokens from any composable. */
object CoSign {
    val colors: CoSignColors
        @Composable get() = LocalCoSignColors.current
}

/**
 * Tabular figures, so amounts line up in lists and do not shift width when they change.
 * Apply to every text that shows money.
 */
const val TABULAR_FIGURES = "tnum"

/**
 * Type scale. Sizes are in `sp` so they follow the user's font size setting; no text
 * sets a fixed height, so layouts must hold at 200% font scale. Body text is at least
 * 14sp with line height of at least 1.3 times its size.
 */
private val CoSignTypography = Typography(
    headlineLarge = TextStyle(fontSize = 30.sp, lineHeight = 36.sp, fontWeight = FontWeight.Bold, letterSpacing = (-0.5).sp),
    headlineMedium = TextStyle(fontSize = 26.sp, lineHeight = 32.sp, fontWeight = FontWeight.Bold, letterSpacing = (-0.4).sp),
    titleMedium = TextStyle(fontSize = 16.sp, lineHeight = 22.sp, fontWeight = FontWeight.SemiBold),
    bodyLarge = TextStyle(fontSize = 16.sp, lineHeight = 22.sp),
    bodyMedium = TextStyle(fontSize = 14.sp, lineHeight = 20.sp),
    labelLarge = TextStyle(fontSize = 15.sp, lineHeight = 20.sp, fontWeight = FontWeight.SemiBold),
    labelMedium = TextStyle(fontSize = 13.sp, lineHeight = 18.sp, fontWeight = FontWeight.SemiBold, letterSpacing = 1.2.sp),
)

/** The app theme: the mockups' light palette, regardless of the system dark setting. */
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
        outline = colors.controlBorder,
        outlineVariant = colors.border,
        error = colors.danger,
    )
    CompositionLocalProvider(LocalCoSignColors provides colors) {
        MaterialTheme(colorScheme = scheme, typography = CoSignTypography, content = content)
    }
}
