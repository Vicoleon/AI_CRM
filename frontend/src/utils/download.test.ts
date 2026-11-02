// The name a download is saved under: the one the server sent in
// Content-Disposition, else a fallback. The Scorecard and VTO template buttons
// share one helper, so the page and the server cannot disagree on what the file
// is called, and the fallback is built from PRODUCT_NAME, never a literal.
//
//   cd <repo>/frontend && node --test "src/utils/download.test.ts"
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { PRODUCT_NAME } from '../brand/names.ts';
import { filenameFromDisposition, templateFilename } from './download.ts';

describe('the name the server sent', () => {
    it('is read from a quoted or a bare filename', () => {
        assert.equal(
            filenameFromDisposition('attachment; filename="Cubie_Sales_Scorecard_template.xlsx"', 'x.xlsx'),
            'Cubie_Sales_Scorecard_template.xlsx',
        );
        assert.equal(filenameFromDisposition('attachment; filename=report.pdf', 'x.pdf'), 'report.pdf');
    });

    it('stops at the end of the value', () => {
        assert.equal(filenameFromDisposition('attachment; filename=a.xlsx; size=10', 'x'), 'a.xlsx');
        assert.equal(filenameFromDisposition('attachment; filename="b.docx"; size=10', 'x'), 'b.docx');
    });

    it('falls back when the header names no file, or there is no header', () => {
        assert.equal(filenameFromDisposition('attachment', 'fallback.xlsx'), 'fallback.xlsx');
        assert.equal(filenameFromDisposition('', 'fallback.xlsx'), 'fallback.xlsx');
        assert.equal(filenameFromDisposition(undefined, 'fallback.xlsx'), 'fallback.xlsx');
        assert.equal(filenameFromDisposition(null, 'fallback.xlsx'), 'fallback.xlsx');
    });
});

describe('the EOS upload templates', () => {
    it('save under the server’s name, team included', () => {
        assert.equal(
            templateFilename('attachment; filename="Cubie_Sales_Scorecard_template.xlsx"', 'Scorecard'),
            'Cubie_Sales_Scorecard_template.xlsx',
        );
        assert.equal(templateFilename('attachment; filename="Cubie_VTO_template.xlsx"', 'VTO'), 'Cubie_VTO_template.xlsx');
    });

    it('fall back to a name built from the product name', () => {
        assert.equal(templateFilename(undefined, 'Scorecard'), `${PRODUCT_NAME}_Scorecard_template.xlsx`);
        assert.equal(templateFilename('attachment', 'VTO'), `${PRODUCT_NAME}_VTO_template.xlsx`);
    });
});
