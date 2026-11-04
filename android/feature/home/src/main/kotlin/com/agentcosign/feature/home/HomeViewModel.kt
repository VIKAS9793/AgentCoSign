package com.agentcosign.feature.home

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.agentcosign.core.domain.ApprovalsRepository
import com.agentcosign.core.model.HomeSnapshot
import dagger.hilt.android.lifecycle.HiltViewModel
import javax.inject.Inject
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.catch
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch

sealed interface HomeUiState {
    data object Loading : HomeUiState
    data class Success(val home: HomeSnapshot) : HomeUiState
    data class Error(val message: String) : HomeUiState
}

@HiltViewModel
class HomeViewModel @Inject constructor(
    private val approvals: ApprovalsRepository,
) : ViewModel() {

    val state: StateFlow<HomeUiState> = approvals.home
        .map<HomeSnapshot, HomeUiState> { HomeUiState.Success(it) }
        .catch { emit(HomeUiState.Error(it.message ?: "Couldn't load your account.")) }
        .stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), HomeUiState.Loading)

    fun setRefundsStopped(stopped: Boolean) {
        viewModelScope.launch { approvals.setRefundsStopped(stopped) }
    }
}
