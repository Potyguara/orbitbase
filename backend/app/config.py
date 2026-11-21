from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "OrbitBase API"
    app_env: str = "development"
    secret_key: str = "change-me"
    access_token_expire_minutes: int = 1440
    database_url: str = "sqlite:///./orbitbase.db"
    # Credenciais Earthdata padrão do servidor (usadas quando o usuário não cadastrou as suas).
    earthdata_username: str | None = None
    earthdata_password: str | None = None
    cors_origins: str = (
        "http://localhost:5500,http://127.0.0.1:5500,https://siscond.online"
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @property
    def cors_origin_list(self) -> list[str]:
        return [
            origin.strip() for origin in self.cors_origins.split(",") if origin.strip()
        ]


settings = Settings()
