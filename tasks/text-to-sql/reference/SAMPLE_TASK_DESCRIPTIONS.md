# Sample task descriptions (for meta-agent context)

These illustrate the *kind* of natural-language-to-SQL problems the target agent
must solve. They are examples of the task family, not the scored questions.

## Example 1 — simple aggregate
- **db_id:** `concert_singer`
- **Schema (excerpt):** `singer(Singer_ID, Name, Country, Age, ...)`
- **Question:** "How many singers do we have?"
- **A correct SQL:** `SELECT count(*) FROM singer`

## Example 2 — filter + projection
- **db_id:** `pets_1`
- **Schema (excerpt):** `student(StuID, LName, Fname, Age, Sex, Major, ...)`,
  `has_pet(StuID, PetID)`, `pets(PetID, PetType, pet_age, weight)`
- **Question:** "Find the first name of students who have a cat as a pet."
- **A correct SQL:**
  `SELECT T1.Fname FROM student AS T1 JOIN has_pet AS T2 ON T1.StuID = T2.StuID
   JOIN pets AS T3 ON T2.PetID = T3.PetID WHERE T3.PetType = 'cat'`

## Example 3 — join + group + order (harder)
- **db_id:** `world_1`
- **Schema (excerpt):** `country(Code, Name, Continent, Population, ...)`,
  `countrylanguage(CountryCode, Language, IsOfficial, Percentage)`
- **Question:** "What are the names of the countries where the official language is English?"
- **A correct SQL:**
  `SELECT T1.Name FROM country AS T1 JOIN countrylanguage AS T2 ON T1.Code = T2.CountryCode
   WHERE T2.Language = 'English' AND T2.IsOfficial = 'T'`

The full task can span single-table aggregates, multi-table joins, grouping,
ordering, nested subqueries, and set operations across diverse database schemas.
