"""add nginx_os_type

Revision ID: 867fa650eacd
Revises: ac044ab840e8
Create Date: 2018-10-15 19:22:08.550777

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '867fa650eacd'
down_revision = 'ac044ab840e8'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table('appconfig', schema=None) as batch_op:
        batch_op.add_column(sa.Column('nginx_os_type', sa.String(length=10), nullable=True))

    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table('appconfig', schema=None) as batch_op:
        batch_op.drop_column('nginx_os_type')

    # ### end Alembic commands ###