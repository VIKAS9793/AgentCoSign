package com.agentcosign.app

import androidx.compose.runtime.Composable
import androidx.lifecycle.viewmodel.navigation3.rememberViewModelStoreNavEntryDecorator
import androidx.navigation3.runtime.NavKey
import androidx.navigation3.runtime.entryProvider
import androidx.navigation3.runtime.rememberNavBackStack
import androidx.navigation3.runtime.rememberSaveableStateHolderNavEntryDecorator
import androidx.navigation3.ui.NavDisplay
import com.agentcosign.feature.home.HomeRoute
import com.agentcosign.feature.onboarding.ConnectPayPalScreen
import com.agentcosign.feature.onboarding.EnrolPhoneScreen
import com.agentcosign.feature.onboarding.LimitsSetupRoute
import com.agentcosign.feature.onboarding.WelcomeScreen
import com.agentcosign.feature.review.ReviewRoute
import com.agentcosign.feature.settings.SettingsRoute
import kotlinx.serialization.Serializable

@Serializable data object Welcome : NavKey
@Serializable data object ConnectPayPal : NavKey
@Serializable data object LimitsSetup : NavKey
@Serializable data object EnrolPhone : NavKey
@Serializable data object Home : NavKey
@Serializable data class Review(val requestId: String) : NavKey
@Serializable data object SettingsKey : NavKey

@Composable
fun CoSignNavigation() {
    val backStack = rememberNavBackStack(Welcome)
    fun go(key: NavKey) { backStack.add(key) }
    fun back() { backStack.removeLastOrNull() }
    fun startAt(key: NavKey) {
        backStack.clear()
        backStack.add(key)
    }

    NavDisplay(
        backStack = backStack,
        onBack = { back() },
        entryDecorators = listOf(
            rememberSaveableStateHolderNavEntryDecorator(),
            rememberViewModelStoreNavEntryDecorator(),
        ),
        entryProvider = entryProvider {
            entry<Welcome> { WelcomeScreen(onGetStarted = { go(ConnectPayPal) }) }
            entry<ConnectPayPal> { ConnectPayPalScreen(onConnect = { go(LimitsSetup) }, onNotNow = { go(LimitsSetup) }, onBack = ::back) }
            entry<LimitsSetup> { LimitsSetupRoute(onContinue = { go(EnrolPhone) }, onBack = ::back) }
            entry<EnrolPhone> { EnrolPhoneScreen(onSetUp = { startAt(Home) }, onNotNow = { startAt(Home) }, onBack = ::back) }
            entry<Home> { HomeRoute(onReview = { go(Review(it)) }, onSettings = { go(SettingsKey) }) }
            entry<Review> { key -> ReviewRoute(key.requestId, onDone = ::back) }
            entry<SettingsKey> { SettingsRoute(onBack = ::back) }
        },
    )
}
