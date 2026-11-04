package com.agentcosign.feature.settings

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.agentcosign.core.domain.SettingsRepository
import com.agentcosign.core.model.Alerts
import com.agentcosign.core.model.Settings
import dagger.hilt.android.lifecycle.HiltViewModel
import javax.inject.Inject
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.catch
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch

sealed interface SettingsUiState {
    data object Loading : SettingsUiState
    data class Success(val settings: Settings) : SettingsUiState
    data class Error(val message: String) : SettingsUiState
}

@HiltViewModel
class SettingsViewModel @Inject constructor(
    private val repository: SettingsRepository,
) : ViewModel() {

    val state: StateFlow<SettingsUiState> = repository.settings
        .map<Settings, SettingsUiState> { SettingsUiState.Success(it) }
        .catch { emit(SettingsUiState.Error(it.message ?: "Couldn't load settings.")) }
        .stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), SettingsUiState.Loading)

    fun setAlerts(alerts: Alerts) {
        viewModelScope.launch { repository.setAlerts(alerts) }
    }

    fun setAssistantEnabled(enabled: Boolean) {
        viewModelScope.launch { repository.setAssistantEnabled(enabled) }
    }
}
