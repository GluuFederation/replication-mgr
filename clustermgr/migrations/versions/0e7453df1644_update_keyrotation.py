"""update keyrotation

Revision ID: 0e7453df1644
Revises: b21895d83725
Create Date: 2018-02-15 19:33:55.817658

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0e7453df1644'
down_revision = 'b21895d83725'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table('keyrotation', schema=None) as batch_op:
        batch_op.add_column(sa.Column('enabled', sa.Boolean(), nullable=True))
        batch_op.drop_column('oxeleven_token_key')
        batch_op.drop_column('oxeleven_url')
        batch_op.drop_column('oxeleven_token')
        batch_op.drop_column('oxeleven_token_iv')

    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table('keyrotation', schema=None) as batch_op:
        batch_op.add_column(sa.Column('oxeleven_token_iv', sa.BLOB(), nullable=True))
        batch_op.add_column(sa.Column('oxeleven_token', sa.BLOB(), nullable=True))
        batch_op.add_column(sa.Column('oxeleven_url', sa.VARCHAR(length=255), nullable=True))
        batch_op.add_column(sa.Column('oxeleven_token_key', sa.BLOB(), nullable=True))
        batch_op.drop_column('enabled')

    # ### end Alembic commands ###