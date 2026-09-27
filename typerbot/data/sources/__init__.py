from typerbot.data.sources.base import ApiSource
from typerbot.data.sources.club_names import ClubNames
from typerbot.data.sources.football_data_csv import FootballDataCsv
from typerbot.data.sources.international import InternationalResults
from typerbot.data.sources.openfootball import OpenFootball
from typerbot.data.sources.openligadb import OpenLigaDb

SOURCE_CLASSES = (FootballDataCsv, OpenFootball, OpenLigaDb, InternationalResults, ClubNames)
SOURCE_LABELS = {cls.name: cls.label for cls in SOURCE_CLASSES}
# Co daje każde źródło (opis w ustawieniach i diagnostyce).
SOURCE_ROLES = {
    FootballDataCsv.name: "główne: nadchodzące mecze z kursami, wyniki, historia z kursami (38 lig, 27 krajów)",
    OpenFootball.name: "terminarz całego sezonu z wyprzedzeniem i wyniki (czołowe ligi Europy, Brazylia i inne)",
    OpenLigaDb.name: "terminarz i wyniki lig niemieckich na bieżąco (Bundesliga 1–3, Puchar Niemiec)",
    InternationalResults.name: "international_results: wyniki reprezentacji – ranking Elo reprezentacji",
    ClubNames.name: "warianty nazw klubów – ujednolicanie nazw drużyn między źródłami",
}

__all__ = ["ApiSource", "ClubNames", "FootballDataCsv", "InternationalResults", "OpenFootball", "OpenLigaDb",
           "SOURCE_CLASSES", "SOURCE_LABELS", "SOURCE_ROLES"]
