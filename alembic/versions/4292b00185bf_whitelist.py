"""whitelist

Revision ID: 4292b00185bf
Revises: 61d0f32eda4b
Create Date: 2020-03-19 12:22:10.280477

"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "4292b00185bf"
down_revision = "61d0f32eda4b"
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table(
        "whitelist",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_name", sa.String(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "approved_automatically",
                "waiting",
                "approved_manually",
                name="whiteliststatus",
            ),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_whitelist_account_name"),
        "whitelist",
        ["account_name"],
        unique=False,
    )
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_index(op.f("ix_whitelist_account_name"), table_name="whitelist")
    op.drop_table("whitelist")
    # ### end Alembic commands ###
