package com.agentcosign.app

import com.agentcosign.core.data.FakeApprovalsRepository
import com.agentcosign.core.data.FakeSettingsRepository
import com.agentcosign.core.domain.ApprovalsRepository
import com.agentcosign.core.domain.SettingsRepository
import dagger.Module
import dagger.Provides
import dagger.hilt.InstallIn
import dagger.hilt.components.SingletonComponent
import java.time.Clock
import javax.inject.Singleton

/** Sample-data wiring. Swap these bindings for gateway-backed ones once the app can sign in. */
@Module
@InstallIn(SingletonComponent::class)
object DataModule {

    @Provides
    @Singleton
    fun clock(): Clock = Clock.systemUTC()

    @Provides
    @Singleton
    fun approvals(clock: Clock): ApprovalsRepository = FakeApprovalsRepository(clock)

    @Provides
    @Singleton
    fun settings(): SettingsRepository = FakeSettingsRepository()
}
