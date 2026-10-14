"""Sample content for trying the portal. Run: python server.py demo

Loads 20 Technical questions and 10 Entrepreneurship questions (3 mandatory), one demo
applicant with a random password, and publishes the exam for 7 days.
Other departments are left empty on purpose: add their questions in the admin portal
(or import a CSV) before assigning applicants to them.
"""
import json
import secrets
import time

import server

# (text, [options], correct_index, difficulty)
TECHNICAL = [
    ("What does HTML stand for?", ["HyperText Markup Language", "High Tech Modern Language", "Hyperlink and Text Markup Logic", "Home Tool Markup Language"], 0, "easy"),
    ("Which data structure follows the Last-In, First-Out (LIFO) principle?", ["Queue", "Stack", "Linked list", "Hash map"], 1, "easy"),
    ("Which Git command creates a new, empty repository?", ["git start", "git new", "git init", "git create"], 2, "easy"),
    ("Which of these Python types is immutable?", ["list", "dict", "set", "tuple"], 3, "easy"),
    ("What is CSS mainly used for?", ["Styling how web pages look", "Storing data on a server", "Running database queries", "Encrypting traffic"], 0, "easy"),
    ("Which HTTP status code means the requested resource was not found?", ["200", "301", "404", "500"], 2, "easy"),
    ("What is the decimal number 5 in binary?", ["011", "101", "110", "111"], 1, "easy"),
    ("Which of the following is a relational database system?", ["Redis", "PostgreSQL", "Elasticsearch", "Kafka"], 1, "easy"),
    ("What is the time complexity of binary search on a sorted array of n items?", ["O(n)", "O(n log n)", "O(log n)", "O(1)"], 2, "medium"),
    ("Which SQL clause filters groups after aggregation?", ["WHERE", "HAVING", "GROUP BY", "ORDER BY"], 1, "medium"),
    ("In REST, which HTTP method is idempotent and typically replaces a resource?", ["POST", "PATCH", "PUT", "CONNECT"], 2, "medium"),
    ("What does git rebase primarily do?", ["Deletes old commits permanently", "Reapplies commits on top of another base commit", "Merges two repositories", "Copies a remote branch"], 1, "medium"),
    ("Which of these is NOT a primitive type in JavaScript?", ["String", "Number", "Boolean", "Object"], 3, "medium"),
    ("What does DNS do?", ["Encrypts web traffic", "Translates domain names to IP addresses", "Assigns IP addresses to devices", "Blocks malicious sites"], 1, "medium"),
    ("Why is an index added to a database column?", ["To speed up lookups at the cost of extra storage and slower writes", "To enforce foreign keys", "To compress the table", "To encrypt the column"], 0, "medium"),
    ("Which best describes a race condition?", ["A program that runs faster on newer hardware", "An outcome that depends on the unsynchronised timing of concurrent operations", "A loop that never terminates", "A memory leak in a long-running process"], 1, "hard"),
    ("What is the worst-case time complexity of quicksort with a naive pivot choice?", ["O(n)", "O(n log n)", "O(n^2)", "O(log n)"], 2, "hard"),
    ("In ACID, what does isolation guarantee?", ["Data survives a crash", "Concurrent transactions do not see each other's intermediate states", "Every transaction is all-or-nothing", "Constraints are never violated"], 1, "hard"),
    ("Which HTTP response header helps prevent clickjacking by controlling framing?", ["X-Frame-Options", "Accept-Language", "ETag", "Content-Length"], 0, "hard"),
    ("What trade-off does the CAP theorem describe?", ["Cost versus performance versus speed", "During a network partition, a distributed store must choose between consistency and availability", "Compression versus accuracy versus latency", "Caching versus persistence"], 1, "hard"),
]
# (text, options, correct, difficulty, mandatory)
ENTREPRENEURSHIP = [
    ("What is a minimum viable product (MVP)?", ["A finished product with every planned feature", "The simplest version that tests the core idea with real users", "A prototype that is never shown to customers", "A marketing brochure"], 1, "medium", 1),
    ("What does bootstrapping a startup mean?", ["Funding it with savings or revenue instead of outside investors", "Hiring a large founding team", "Selling to other startups only", "Listing on a stock exchange"], 0, "medium", 1),
    ("What is a value proposition?", ["The price list of a company", "A clear statement of the benefit a product gives a specific customer", "The company's annual profit", "A legal contract with suppliers"], 1, "easy", 1),
    ("In startup terms, what is a pivot?", ["Closing the company", "A change in strategy based on what you have learned", "Hiring a new CEO", "Moving to a new office"], 1, "medium", 0),
    ("What is a company's burn rate?", ["How fast it gains customers", "How fast it spends its cash", "How fast it ships features", "Its employee turnover"], 1, "easy", 0),
    ("Which of the following is a recurring revenue model?", ["One-time donation", "Subscription fees", "Selling a single asset", "A refund"], 1, "easy", 0),
    ("What does product-market fit mean?", ["The product is cheaper than every rival", "The product satisfies strong demand in a defined market", "The product is sold in many countries", "The product has a patent"], 1, "medium", 0),
    ("Which best describes an angel investor?", ["A bank that issues loans", "An individual who invests personal funds in early-stage startups", "A government grant officer", "A customer who pre-orders"], 1, "easy", 0),
    ("What does B2B stand for?", ["Back to basics", "Business to business", "Brand to buyer", "Budget to balance"], 1, "easy", 0),
    ("What does a SWOT analysis examine?", ["Sales, Wages, Orders, Taxes", "Strengths, Weaknesses, Opportunities, Threats", "Suppliers, Workers, Outputs, Targets", "Savings, Wealth, Operations, Trade"], 1, "hard", 0),
]


def run():
    db = server.connect()
    try:
        now = int(time.time())
        tech = db.execute("SELECT id FROM departments WHERE name='Technical'").fetchone()
        if not tech:
            raise SystemExit("The 'Technical' department is missing.")
        if db.execute("SELECT COUNT(*) FROM questions").fetchone()[0]:
            raise SystemExit("Questions already exist; demo data was not loaded to avoid duplicates.")
        with server.Tx(db):
            for text, opts, corr, diff in TECHNICAL:
                db.execute("INSERT INTO questions(category,dept_id,text,opt1,opt2,opt3,opt4,correct,difficulty,created_at) VALUES('department',?,?,?,?,?,?,?,?,?)",
                           (tech["id"], text, *opts, corr, diff, now))
            for text, opts, corr, diff, mand in ENTREPRENEURSHIP:
                db.execute("INSERT INTO questions(category,dept_id,text,opt1,opt2,opt3,opt4,correct,difficulty,mandatory,created_at) VALUES('entrepreneurship',NULL,?,?,?,?,?,?,?,?,?)",
                           (text, *opts, corr, diff, mand, now))
            db.execute("UPDATE config SET status='published', opens_at=?, closes_at=? WHERE id=1", (now - 60, now + 7 * 86400))
        pw = secrets.token_urlsafe(9)
        email = "demo.applicant@example.com"
        try:
            server.create_user(db, "applicant", email, "Demo Applicant", pw, tech["id"])
        except server.ApiError:
            print("Demo applicant already exists; password unchanged.")
            return
        print("Demo data loaded. Exam published for 7 days.")
        print(f"Demo applicant login:  {email}  /  {pw}")
    finally:
        db.close()
