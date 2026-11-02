from pydantic import BaseModel


class User(BaseModel):
    id: str
    email: str
    name: str
    department: str
    roles: list[str] = []

    @property
    def is_admin(self) -> bool:
        return "admin" in self.roles
