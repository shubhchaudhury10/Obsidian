"""add chunks table (pgvector) for Reddit RAG; enable vector extension

Replaces ChromaDB: Reddit chunks are embedded with Gemini gemini-embedding-001 (768-dim)
and stored here, cosine-searched via pgvector.

Revision ID: c2f7a9d10b34
Revises: b8038a261297
Create Date: 2026-08-27

"""
from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector


# revision identifiers, used by Alembic.
revision = 'c2f7a9d10b34'
down_revision = 'b8038a261297'
branch_labels = None
depends_on = None


def upgrade():
    # The vector type must exist before any column can use it.
    op.execute('CREATE EXTENSION IF NOT EXISTS vector')

    op.create_table(
        'chunks',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('product_slug', sa.String(length=255), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('source', sa.String(length=1000), nullable=True),
        sa.Column('score', sa.Integer(), nullable=True),
        sa.Column('embedding', Vector(768), nullable=False),
        sa.Column('ingested_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_chunks_product_slug', 'chunks', ['product_slug'])
    op.create_index('ix_chunks_ingested_at', 'chunks', ['ingested_at'])
    # Approximate-nearest-neighbour index for cosine distance (pgvector `<=>`).
    op.execute(
        'CREATE INDEX ix_chunks_embedding ON chunks '
        'USING hnsw (embedding vector_cosine_ops)'
    )


def downgrade():
    op.drop_index('ix_chunks_embedding', table_name='chunks')
    op.drop_index('ix_chunks_ingested_at', table_name='chunks')
    op.drop_index('ix_chunks_product_slug', table_name='chunks')
    op.drop_table('chunks')
    # Leave the `vector` extension installed (other objects may rely on it).
