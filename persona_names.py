"""注册资料用的英文姓名池。

不依赖 Faker：Faker 不在 requirements 里，生产环境装不装全看运气，
之前就是缺装导致 fallback 到 3 个名字、批量撞 Jordan。

约 150 名 × 150 姓 ≈ 2 万+ 种组合，够号池规模用了。
"""
from __future__ import annotations

import random

FIRST_NAMES = (
    "James", "Mary", "Robert", "Patricia", "John", "Jennifer", "Michael",
    "Linda", "David", "Elizabeth", "William", "Barbara", "Richard", "Susan",
    "Joseph", "Jessica", "Thomas", "Sarah", "Charles", "Karen", "Christopher",
    "Lisa", "Daniel", "Nancy", "Matthew", "Margaret", "Anthony", "Betty",
    "Mark", "Sandra", "Donald", "Ashley", "Steven", "Kimberly", "Paul",
    "Emily", "Andrew", "Donna", "Joshua", "Michelle", "Kevin", "Carol",
    "Brian", "Amanda", "George", "Dorothy", "Timothy", "Melissa", "Ronald",
    "Deborah", "Edward", "Stephanie", "Jason", "Rebecca", "Jeffrey", "Sharon",
    "Ryan", "Laura", "Jacob", "Cynthia", "Gary", "Kathleen", "Nicholas",
    "Amy", "Eric", "Angela", "Jonathan", "Shirley", "Stephen", "Anna",
    "Larry", "Brenda", "Justin", "Pamela", "Scott", "Emma", "Brandon",
    "Nicole", "Benjamin", "Helen", "Samuel", "Samantha", "Gregory",
    "Katherine", "Alexander", "Christine", "Patrick", "Debra", "Frank",
    "Rachel", "Raymond", "Carolyn", "Jack", "Janet", "Dennis", "Maria",
    "Jerry", "Olivia", "Tyler", "Heather", "Aaron", "Diane", "Jose",
    "Julie", "Adam", "Joyce", "Nathan", "Victoria", "Henry", "Ruth",
    "Zachary", "Virginia", "Douglas", "Lauren", "Peter", "Kelly", "Kyle",
    "Christina", "Noah", "Joan", "Ethan", "Evelyn", "Jeremy", "Judith",
    "Walter", "Andrea", "Christian", "Hannah", "Keith", "Megan", "Roger",
    "Cheryl", "Terry", "Jacqueline", "Austin", "Martha", "Sean", "Madison",
    "Gerald", "Teresa", "Carl", "Gloria", "Harold", "Sara", "Dylan",
    "Janice", "Arthur", "Ann", "Lawrence", "Kathryn", "Jordan", "Abigail",
    "Bryan", "Sophia", "Alan", "Isabella", "Juan", "Ava", "Logan",
    "Charlotte", "Wayne", "Mia", "Ralph", "Amelia", "Billy", "Harper",
    "Bruce", "Evelyn",
)

LAST_NAMES = (
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
    "Davis", "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez",
    "Wilson", "Anderson", "Thomas", "Taylor", "Moore", "Jackson", "Martin",
    "Lee", "Perez", "Thompson", "White", "Harris", "Sanchez", "Clark",
    "Ramirez", "Lewis", "Robinson", "Walker", "Young", "Allen", "King",
    "Wright", "Scott", "Torres", "Nguyen", "Hill", "Flores", "Green",
    "Adams", "Nelson", "Baker", "Hall", "Rivera", "Campbell", "Mitchell",
    "Carter", "Roberts", "Gomez", "Phillips", "Evans", "Turner", "Diaz",
    "Parker", "Cruz", "Edwards", "Collins", "Reyes", "Stewart", "Morris",
    "Morales", "Murphy", "Cook", "Rogers", "Morgan", "Cooper", "Peterson",
    "Bailey", "Reed", "Kelly", "Howard", "Ramos", "Kim", "Cox", "Ward",
    "Richardson", "Watson", "Brooks", "Chavez", "Wood", "Bennett", "Gray",
    "Mendoza", "Ruiz", "Hughes", "Price", "Alvarez", "Castillo", "Sanders",
    "Patel", "Myers", "Long", "Ross", "Foster", "Jimenez", "Powell",
    "Jenkins", "Perry", "Russell", "Sullivan", "Bell", "Coleman", "Butler",
    "Henderson", "Barnes", "Gonzales", "Fisher", "Vasquez", "Simmons",
    "Romero", "Patterson", "Alexander", "Hamilton", "Graham", "Reynolds",
    "Griffin", "Wallace", "Moreno", "West", "Cole", "Hayes", "Bryant",
    "Herrera", "Gibson", "Ellis", "Tran", "Medina", "Aguilar", "Stevens",
    "Murray", "Ford", "Castro", "Marshall", "Owens", "Harrison",
    "Fernandez", "Mcdonald", "Woods", "Washington", "Kennedy", "Wells",
)


def random_full_name(rng: random.Random = random) -> str:
    """返回 ``名 姓`` 形式的随机英文全名。"""
    return f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"
