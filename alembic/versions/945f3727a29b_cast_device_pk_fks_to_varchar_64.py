"""cast device PK & FKs to VARCHAR(64)

Revision ID: 945f3727a29b
Revises: 23624dd4535e
Create Date: 2025-05-02 19:52:42.997331

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '945f3727a29b'
down_revision: Union[str, None] = '23624dd4535e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
