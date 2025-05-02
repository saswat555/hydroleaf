"""device id to uuid string

Revision ID: 488056479ffb
Revises: 9f2fa6f4ff1e
Create Date: 2025-05-02 13:52:27.079739

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '488056479ffb'
down_revision: Union[str, None] = '9f2fa6f4ff1e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
