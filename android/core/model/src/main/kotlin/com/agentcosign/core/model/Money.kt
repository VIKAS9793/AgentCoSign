package com.agentcosign.core.model

import java.math.BigDecimal
import java.math.RoundingMode
import java.text.NumberFormat
import java.util.Currency
import java.util.Locale

/** An exact amount in one currency. Never a floating-point number. */
data class Money(val amount: BigDecimal, val currency: String = "USD") : Comparable<Money> {

    init {
        require(amount.scale() <= 2) { "Money has at most two decimal places" }
    }

    operator fun plus(other: Money): Money = same(other) { Money(amount + other.amount, currency) }

    operator fun minus(other: Money): Money = same(other) { Money(amount - other.amount, currency) }

    override fun compareTo(other: Money): Int = same(other) { amount.compareTo(other.amount) }

    fun display(locale: Locale = Locale.US): String =
        NumberFormat.getCurrencyInstance(locale).apply {
            currency = Currency.getInstance(this@Money.currency)
        }.format(amount.setScale(2, RoundingMode.UNNECESSARY))

    private inline fun <T> same(other: Money, block: () -> T): T {
        require(currency == other.currency) { "Cannot mix $currency and ${other.currency}" }
        return block()
    }

    companion object {
        fun usd(value: String): Money = Money(BigDecimal(value).setScale(2, RoundingMode.UNNECESSARY))
        val ZERO: Money = usd("0")
    }
}
