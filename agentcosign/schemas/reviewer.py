"""Request schema for starting a reviewer session."""

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class ReviewerSessionRequest(BaseModel):
    """The shared reviewer token, exchanged once for an HttpOnly cookie."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    token: SecretStr = Field(min_length=1, max_length=256)
