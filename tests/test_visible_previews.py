"""Visible task images reference one verified model, without weakening proof."""

import hashlib
import io
import contextlib
from pathlib import Path
import unittest
from unittest.mock import patch

from charmm_gui_cli.cli import main
from charmm_gui_cli.chem.preview import render_model
from charmm_gui_cli.pipeline.preview import preview_run
from charmm_gui_cli.pipeline.managed import _final_summary
from charmm_gui_cli.presentation.reporting import verified_images
from charmm_gui_cli.store.auth import ToolError
from charmm_gui_cli.store.paths import read_value
import test_single_directory as fixtures
from test_preview import atom


class VisiblePreviewTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SingleDirectoryTests()
        # Bind actual tiny PDB coordinates to the fixture archive/receipt before
        # cached evidence is created. This is not a scientific model acceptance.
        structure = ('\n'.join([atom(1,'CA','ALA',(0,0,0)), atom(2,'C1','LIG',(0,3,0),chain='L'),
                                atom(3,'O1','LIG',(1.4,3,0),chain='L',element='O')])
                     + '\nCONECT    2    3\nEND\n').encode()
        archive = fixtures.provenance.ManagedProvenanceTests.write_archive
        def coordinates(fixture, identifier, payload=b'archive fixture'):
            archive(fixture, identifier, structure)
        with patch.object(fixtures.provenance.ManagedProvenanceTests,'write_archive',coordinates):
            self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root, self.run = self.fixture.root, self.fixture.run

    def test_automatic_preview_is_visible_without_a_second_coordinate_copy(self):
        before = {path: path.read_bytes() for path in self.root.rglob('*.pdb')}
        with patch('requests.Session.request', side_effect=AssertionError('Preview stays offline')):
            result = preview_run(self.run, width=400)
            self.assertFalse(result['model_copied'])
            self.assertEqual(result['model'], result['source'])
            self.assertTrue(Path(result['model']).is_relative_to(self.root/'model'))
            self.assertTrue(result['images'])
            self.assertTrue(all(Path(item['file']).is_relative_to(self.root/'previews') for item in result['images']))
            self.assertEqual(preview_run(self.run, width=400), result)
            images, _ = verified_images(self.run, self.fixture.report)
            self.assertEqual(len(images), len(result['images']))
        self.assertEqual(before, {path: path.read_bytes() for path in self.root.rglob('*.pdb')})
        self.assertFalse((self.run/'results/previews').exists())

    def test_edited_picture_is_preserved_and_does_not_invalidate_model(self):
        first = preview_run(self.run, width=400)
        path = Path(first['images'][0]['file'])
        path.write_bytes(b'user edited picture')
        second = preview_run(self.run, width=400)
        self.assertEqual(path.read_bytes(), b'user edited picture')
        self.assertEqual(first['model'], second['model'])
        self.assertNotEqual(first['images'][0]['file'], second['images'][0]['file'])
        self.assertTrue(_final_summary(self.run)['passed'])
        self.assertTrue(verified_images(self.run, self.fixture.report)[0])

    def test_explicit_output_still_copies_model_and_does_not_replace_default_cache(self):
        default = preview_run(self.run, width=400)
        before = (self.run/'results/preview.json').read_bytes()
        result = preview_run(self.run, out=self.root/'selected-images', width=400)
        self.assertTrue(result['model_copied'])
        self.assertEqual(Path(result['model']), self.root/'selected-images/model.pdb')
        self.assertEqual(Path(result['model']).read_bytes(), Path(default['model']).read_bytes())
        self.assertEqual((self.run/'results/preview.json').read_bytes(), before)

    def test_symlinked_visible_destination_cannot_write_outside_task(self):
        elsewhere = self.root.parent/'foreign-preview-folder'
        elsewhere.mkdir()
        (elsewhere/'keep').write_text('foreign')
        (self.root/'previews').symlink_to(elsewhere, target_is_directory=True)
        with self.assertRaises(ToolError) as error:
            preview_run(self.run, width=400)
        self.assertEqual(error.exception.key, 'preview.public_path_invalid')
        self.assertEqual(sorted(p.name for p in elsewhere.iterdir()), ['keep'])

    def test_visible_cache_survives_task_move_without_rewriting_scientific_evidence(self):
        first = preview_run(self.run, width=400)
        evidence = (self.run/'results/validation.json').read_bytes()
        original_files = {Path(item['file']).relative_to(self.root): hashlib.sha256(Path(item['file']).read_bytes()).hexdigest()
                          for item in first['images']}
        moved = self.root.with_name('moved preview task')
        self.root.rename(moved)
        result = preview_run(moved/'.charmm-gui', width=400)
        self.assertTrue(Path(result['model']).is_relative_to(moved/'model'))
        self.assertEqual(evidence, (moved/'.charmm-gui/results/validation.json').read_bytes())
        self.assertEqual(original_files, {Path(item['file']).relative_to(moved): item['sha256'] for item in result['images']})
        report = read_value(moved/'.charmm-gui/results/validation.json')
        self.assertTrue(verified_images(moved/'.charmm-gui', report)[0])

    def test_public_tty_and_html_report_find_visible_images(self):
        from charmm_gui_cli.locales.i18n import set_language
        self.addCleanup(set_language)
        result = preview_run(self.run, width=400)
        for language in ('en','zh'):
            output, errors = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors), patch.object(output, 'isatty', return_value=True):
                code = main(['--lang',language,'preview',str(self.root),'--width','400'])
            self.assertEqual(code,0,errors.getvalue())
            self.assertIn('/previews/',output.getvalue())
            self.assertNotIn('.charmm-gui/results/previews',output.getvalue())
            destination = self.root/('report-'+language+'.html')
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(errors):
                code = main(['--lang',language,'jobs','report',str(self.root),'--format','html','--out',str(destination)])
            self.assertEqual(code,0,errors.getvalue())
            self.assertIn('data:image/png;base64,',destination.read_text())
        self.assertEqual(result,preview_run(self.run,width=400))

    def test_reference_mode_is_explicit_and_rejects_invalid_values_without_output(self):
        source = next((self.root/'model').rglob('step5_assembly.pdb'))
        destination = self.root/'invalid-preview'
        with self.assertRaises(ToolError) as error:
            render_model(source,destination,copy_model='false')
        self.assertEqual(error.exception.key,'preview.copy_mode_invalid')
        self.assertFalse(destination.exists())


if __name__=='__main__':
    unittest.main()
