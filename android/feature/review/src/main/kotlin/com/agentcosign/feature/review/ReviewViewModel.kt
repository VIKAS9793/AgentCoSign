package com.agentcosign.feature.review

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.agentcosign.core.domain.ApprovalsRepository
import com.agentcosign.core.model.Decision
import com.agentcosign.core.model.DecisionResult
import com.agentcosign.core.model.PendingRequest
import dagger.assisted.Assisted
import dagger.assisted.AssistedFactory
import dagger.assisted.AssistedInject
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

sealed interface ReviewUiState {
    data object Loading : ReviewUiState
    data object Empty : ReviewUiState
    data class Ready(val request: PendingRequest, val deciding: Boolean = false, val refusal: String? = null) : ReviewUiState
    data class Decided(val result: DecisionResult.Done) : ReviewUiState
}

@HiltViewModel(assistedFactory = ReviewViewModel.Factory::class)
class ReviewViewModel @AssistedInject constructor(
    @Assisted private val requestId: String,
    private val approvals: ApprovalsRepository,
) : ViewModel() {

    @AssistedFactory
    interface Factory {
        fun create(requestId: String): ReviewViewModel
    }

    private val _state = MutableStateFlow<ReviewUiState>(ReviewUiState.Loading)
    val state: StateFlow<ReviewUiState> = _state.asStateFlow()

    init {
        viewModelScope.launch {
            _state.value = approvals.request(requestId)?.let { ReviewUiState.Ready(it) } ?: ReviewUiState.Empty
        }
    }

    /**
     * The real gateway needs a passkey assertion bound to this request and decision.
     * Until the app can produce one, the decision goes to the repository unsigned,
     * which only the sample-data repository accepts.
     */
    fun decide(decision: Decision) {
        val ready = _state.value as? ReviewUiState.Ready ?: return
        if (ready.deciding) return
        _state.value = ready.copy(deciding = true, refusal = null)
        viewModelScope.launch {
            when (val result = approvals.decide(requestId, decision)) {
                is DecisionResult.Done -> _state.value = ReviewUiState.Decided(result)
                is DecisionResult.Refused -> _state.update { ready.copy(deciding = false, refusal = result.reason) }
            }
        }
    }
}
