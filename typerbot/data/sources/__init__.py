from typerbot.data.sources.api_football import ApiFootball
from typerbot.data.sources.base import ApiSource
from typerbot.data.sources.football_data_csv import FootballDataCsv
from typerbot.data.sources.football_data_org import FootballDataOrg
from typerbot.data.sources.oddspapi import OddsPapi
from typerbot.data.sources.the_odds_api import TheOddsApi

SOURCE_LABELS = {cls.name: cls.label for cls in (FootballDataCsv, FootballDataOrg, ApiFootball, TheOddsApi, OddsPapi)}

__all__ = ["ApiSource", "ApiFootball", "FootballDataCsv", "FootballDataOrg", "OddsPapi", "SOURCE_LABELS",
           "TheOddsApi"]
