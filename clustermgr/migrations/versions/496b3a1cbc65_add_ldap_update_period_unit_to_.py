"""add ldap_update_period_unit to AppConfiguration

Revision ID: 496b3a1cbc65
Revises: 867fa650eacd
Create Date: 2018-10-18 17:26:24.334215

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '496b3a1cbc65'
down_revision = '867fa650eacd'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table('appconfig', schema=None) as batch_op:
        batch_op.add_column(sa.Column('ldap_update_period_unit', sa.String(length=1), nullable=True))

    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table('appconfig', schema=None) as batch_op:
        batch_op.drop_column('ldap_update_period_unit')

    # ### end Alembic commands ###
