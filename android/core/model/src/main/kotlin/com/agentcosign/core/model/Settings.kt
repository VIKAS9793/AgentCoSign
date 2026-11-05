package com.agentcosign.core.model

import java.time.Duration

/** The owner's money limits, as configured on the gateway. Shown, never edited, by the app. */
data class Limits(
    val automaticUpTo: Money,
    val approvalUpTo: Money,
    val dailyMost: Money,
    val perCustomerCount: Int,
    val perCustomerWindow: Duration,
    val timeToDecide: Duration,
)

data class Suggester(val name: String, val role: String, val isAssistant: Boolean, val enabled: Boolean)

data class ApproverDevice(val label: String, val active: Boolean)

data class Alerts(val refundWaiting: Boolean, val trickMessage: Boolean, val dailySummary: Boolean)

data class PayPalLink(val accountEnding: String, val connected: Boolean)

data class Settings(
    val limits: Limits,
    val suggesters: List<Suggester>,
    val devices: List<ApproverDevice>,
    val alerts: Alerts,
    val paypal: PayPalLink,
)
