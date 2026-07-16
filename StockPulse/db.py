import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

# PostgreSQL is the default runtime database; override with STOCKPULSE_DATABASE_URL when needed.
DATABASE_URL = os.getenv("STOCKPULSE_DATABASE_URL", "postgresql+psycopg://stockpulse:stockpulse@localhost:5432/stockpulse")

engine_kwargs = {"future": True}
if DATABASE_URL.startswith("sqlite"):
    engine_kwargs["connect_args"] = {"check_same_thread": False}
else:
    engine_kwargs["pool_pre_ping"] = True

engine = create_engine(DATABASE_URL, **engine_kwargs)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()

def init_db():
    Base.metadata.create_all(bind=engine)
    
    # Seed dummy data
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        from models import User, Supplier, Product, Batch
        from datetime import date
        # Check if users already exist
        if db.query(User).count() == 0:
            from passlib.context import CryptContext
            pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
            
            # 1. Create Suppliers
            acme = Supplier(name="Acme Electronics", contact_email="supply@acme.com", lead_time_days=5)
            chair_co = Supplier(name="ErgoSeat Co", contact_email="sales@ergoseat.com", lead_time_days=10)
            tea_garden = Supplier(name="GreenTea Gardens", contact_email="tea@greentea.com", lead_time_days=4)
            db.add_all([acme, chair_co, tea_garden])
            db.commit()
            
            # 2. Create Users (admin, supplier, standard user)
            admin = User(
                username="admin",
                email="admin@stockpulse.com",
                hashed_password=pwd_context.hash("admin12345"),
                role="admin",
                is_active=True,
                is_verified=True
            )
            acme_user = User(
                username="acme_supplier",
                email="acme@stockpulse.com",
                hashed_password=pwd_context.hash("supplier12345"),
                role="supplier",
                is_active=True,
                is_verified=True,
                supplier_id=acme.id
            )
            demouser = User(
                username="demouser",
                email="demouser@stockpulse.com",
                hashed_password=pwd_context.hash("user12345"),
                role="user",
                is_active=True,
                is_verified=True
            )
            db.add_all([admin, acme_user, demouser])
            
            # 3. Create Products
            earbuds = Product(sku="EAR-WIRELESS-01", name="Premium Wireless Earbuds", category="Electronics", safety_stock=10, supplier_id=acme.id)
            chair = Product(sku="CHAIR-ERGONOMIC-02", name="Ergonomic Office Chair", category="Furniture", safety_stock=5, supplier_id=chair_co.id)
            tea = Product(sku="TEA-GREEN-03", name="Organic Green Tea Pack", category="Beverages", safety_stock=20, supplier_id=tea_garden.id)
            bottle = Product(sku="BOTTLE-STEEL-04", name="Stainless Steel Water Bottle", category="Kitchenware", safety_stock=15, supplier_id=acme.id)
            db.add_all([earbuds, chair, tea, bottle])
            db.commit()
            
            # 4. Create Batches
            b1 = Batch(product_id=earbuds.id, batch_number="B-EAR-001", quantity_received=50, quantity_remaining=35, expiry_date=date(2027, 12, 31))
            b2 = Batch(product_id=chair.id, batch_number="B-CHR-002", quantity_received=10, quantity_remaining=8, expiry_date=date(2030, 1, 1))
            b3 = Batch(product_id=tea.id, batch_number="B-TEA-003", quantity_received=100, quantity_remaining=12, expiry_date=date(2026, 11, 30)) # Low stock!
            b4 = Batch(product_id=bottle.id, batch_number="B-BTL-004", quantity_received=25, quantity_remaining=3, expiry_date=date(2028, 6, 15)) # Low stock!
            db.add_all([b1, b2, b3, b4])
            db.commit()
            print("Successfully seeded database with dummy demo items!")
    except Exception as e:
        db.rollback()
        print(f"Error seeding database: {e}")
    finally:
        db.close()

