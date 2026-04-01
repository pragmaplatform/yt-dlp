#!/usr/bin/env python3

# Allow direct execution
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


from yt_dlp import YoutubeDL
from yt_dlp.extractor import YoutubeIE


class TestYoutubeMisc(unittest.TestCase):
    def test_youtube_extract(self):
        assertExtractId = lambda url, video_id: self.assertEqual(YoutubeIE.extract_id(url), video_id)
        assertExtractId('http://www.youtube.com/watch?&v=BaW_jenozKc', 'BaW_jenozKc')
        assertExtractId('https://www.youtube.com/watch?&v=BaW_jenozKc', 'BaW_jenozKc')
        assertExtractId('https://www.youtube.com/watch?feature=player_embedded&v=BaW_jenozKc', 'BaW_jenozKc')
        assertExtractId('https://www.youtube.com/watch_popup?v=BaW_jenozKc', 'BaW_jenozKc')
        assertExtractId('http://www.youtube.com/watch?v=BaW_jenozKcsharePLED17F32AD9753930', 'BaW_jenozKc')
        assertExtractId('BaW_jenozKc', 'BaW_jenozKc')

    def test_youtube_game_attribute_prefers_browse_endpoint(self):
        """Knowledge-graph cards (e.g. people) precede game cards; prefer the linked game row."""
        ie = YoutubeIE(YoutubeDL({'quiet': True}))
        hitman_on_tap = {
            'innertubeCommand': {
                'commandMetadata': {'webCommandMetadata': {
                    'url': '/channel/UCQuo9IHqqME6ek67fdqGO2w'}},
                'browseEndpoint': {'browseId': 'UCQuo9IHqqME6ek67fdqGO2w'},
            },
        }
        data = {
            'engagementPanels': [{
                'engagementPanelSectionListRenderer': {
                    'content': {
                        'structuredDescriptionContentRenderer': {
                            'items': [{
                                'videoAttributesSectionViewModel': {
                                    'videoAttributeViewModels': [
                                        {'videoAttributeViewModel': {
                                            'title': 'Bruce Lee',
                                            'subtitle': 'Martial artist and actor',
                                        }},
                                        {'videoAttributeViewModel': {
                                            'title': 'Hitman World of Assassination',
                                            'subtitle': '2022',
                                            'onTap': hitman_on_tap,
                                        }},
                                    ],
                                },
                            }],
                        },
                    },
                },
            }],
        }
        game_info = ie._extract_game_from_video_attributes_section(data)
        self.assertEqual(game_info['game'], 'Hitman World of Assassination')
        self.assertEqual(game_info['game_release_year'], '2022')
        self.assertIn('UCQuo9IHqqME6ek67fdqGO2w', game_info['game_url'])


if __name__ == '__main__':
    unittest.main()
