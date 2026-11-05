package com.agentcosign.feature.onboarding

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.agentcosign.core.domain.SettingsRepository
import com.agentcosign.core.model.Limits
import dagger.hilt.android.lifecycle.HiltViewModel
import javax.inject.Inject
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.catch
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.flow.stateIn

/** What the limits screen shows. Limits come from the gateway and are never edited here. */
sealed interface LimitsUiState {
    data object Loading : LimitsUiState
    data class Success(val limits: Limits) : LimitsUiState
    data class Error(val message: String) : LimitsUiState
}

/**
 * Exposes the gateway's limits so the owner sees what can move on its own.
 *
 * @property state the current limits, or why they could not be read.
 */
@HiltViewModel
class LimitsViewModel @Inject constructor(
    settings: SettingsRepository,
) : ViewModel() {

    val state: StateFlow<LimitsUiState> = settings.settings
        .map<_, LimitsUiState> { LimitsUiState.Success(it.limits) }
        .catch { emit(LimitsUiState.Error("Couldn't read your limits.")) }
        .stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), LimitsUiState.Loading)
}
