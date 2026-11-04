package com.agentcosign.feature.onboarding

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.agentcosign.core.domain.SettingsRepository
import com.agentcosign.core.model.Money
import dagger.hilt.android.lifecycle.HiltViewModel
import java.math.BigDecimal
import java.math.RoundingMode
import javax.inject.Inject
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

data class LimitsSetupUiState(
    val automatic: String = "",
    val daily: String = "",
    val error: String? = null,
    val saving: Boolean = false,
)

@HiltViewModel
class LimitsSetupViewModel @Inject constructor(
    private val settings: SettingsRepository,
) : ViewModel() {

    private val _state = MutableStateFlow(LimitsSetupUiState())
    val state: StateFlow<LimitsSetupUiState> = _state.asStateFlow()

    private val _saved = MutableSharedFlow<Unit>()
    val saved: SharedFlow<Unit> = _saved.asSharedFlow()

    init {
        viewModelScope.launch {
            val limits = settings.settings.first().limits
            _state.update { it.copy(automatic = limits.automaticUpTo.amount.toPlainString(), daily = limits.dailyMost.amount.toPlainString()) }
        }
    }

    fun onAutomaticChange(value: String) = _state.update { it.copy(automatic = value, error = null) }

    fun onDailyChange(value: String) = _state.update { it.copy(daily = value, error = null) }

    fun save() {
        val automatic = parse(_state.value.automatic)
        val daily = parse(_state.value.daily)
        if (automatic == null || daily == null) {
            _state.update { it.copy(error = "Enter amounts like 25.00") }
            return
        }
        viewModelScope.launch {
            _state.update { it.copy(saving = true) }
            val current = settings.settings.first().limits
            val approval = if (current.approvalUpTo < automatic) automatic else current.approvalUpTo
            val result = settings.saveLimits(
                current.copy(automaticUpTo = automatic, approvalUpTo = minOf(approval, daily), dailyMost = daily),
            )
            _state.update { it.copy(saving = false, error = result.exceptionOrNull()?.message) }
            if (result.isSuccess) _saved.emit(Unit)
        }
    }

    private fun parse(text: String): Money? = text.trim().replace(",", "").toBigDecimalOrNull()
        ?.takeIf { it.scale() <= 2 && it.signum() >= 0 }
        ?.let { Money(it.setScale(2, RoundingMode.UNNECESSARY)) }

    private fun String.toBigDecimalOrNull(): BigDecimal? = runCatching { BigDecimal(this) }.getOrNull()
}
