"""
Accuracy evaluation script - compares extracted fields against ground truth.
Usage: python evaluate_accuracy.py
"""
import json
import os
import re
from pathlib import Path

BASE_DIR = Path(__file__).parent

def load_json(path):
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)

def match_exact(extracted, expected):
    return extracted.lower().strip() == expected.lower().strip()

def match_contains_keywords(extracted, expected_keywords):
    """Check if extracted text contains all expected keywords (case-insensitive)"""
    if not extracted:
        return False
    ext_lower = extracted.lower()
    keywords = [kw.lower().strip() for kw in expected_keywords.split(',')]
    return all(kw in ext_lower for kw in keywords)

def evaluate_product(extracted_product, annotation, rules):
    """Evaluate a single product against ground truth annotation"""
    scores = {}
    details = {}

    for field, rule in rules.items():
        weight = rule['weight']
        match_type = rule['match']
        expected = annotation.get('expected', {}).get(field)

        if expected is None:
            continue

        if match_type == 'exact':
            ext_value = extracted_product.get(field, {}).get('value', '') if isinstance(extracted_product.get(field), dict) else str(extracted_product.get(field, ''))
            is_match = match_exact(ext_value, expected)
            details[field] = {'extracted': ext_value, 'expected': expected, 'match': is_match}
        elif match_type == 'contains_keywords':
            # expected is either a string of comma-separated keywords or a field name to look up
            if isinstance(expected, str):
                if field.endswith('_contains'):
                    actual_field = field.replace('_contains', '')
                else:
                    actual_field = field
                ext_value = extracted_product.get(actual_field, {}).get('value', '') if isinstance(extracted_product.get(actual_field), dict) else ''
                is_match = match_contains_keywords(ext_value, expected)
                details[field] = {'extracted': ext_value[:80], 'expected_keywords': expected, 'match': is_match}
            elif isinstance(expected, list):
                ext_value = extracted_product.get(field, {}).get('value', '') if isinstance(extracted_product.get(field), dict) else ''
                is_match = all(kw.lower() in ext_value.lower() for kw in expected)
                details[field] = {'extracted': ext_value[:80], 'expected_keywords': expected, 'match': is_match}
        else:
            continue

        scores[field] = {'weight': weight, 'match': is_match}

    # Calculate weighted score
    total_weight = sum(s['weight'] for s in scores.values())
    weighted_score = sum(s['weight'] for s in scores.values() if s['match']) / max(total_weight, 0.01)

    return weighted_score, scores, details


def main():
    print("=" * 70)
    print("  MVP Accuracy Evaluation - Extracted vs Ground Truth")
    print("=" * 70)

    all_results = []

    for pdf_name in ['01', '02', '03']:
        gt_path = BASE_DIR / f'{pdf_name}_ground_truth.json'
        fields_path = BASE_DIR.parent / 'step5_output_v2' / pdf_name / f'{pdf_name}_fields.json'

        if not gt_path.exists():
            print(f"\n  [{pdf_name}] Ground truth not found: {gt_path}")
            continue
        if not fields_path.exists():
            print(f"\n  [{pdf_name}] Fields not found: {fields_path}")
            continue

        gt_data = load_json(gt_path)
        fields_data = load_json(fields_path)
        rules = gt_data.get('evaluation_rules', {})

        print(f"\n── {pdf_name}.pdf ──")
        print(f"  {gt_data.get('description', 'N/A')}")
        print(f"  Ground truth products: {len(gt_data['annotations'])}")
        print(f"  Extracted products: {fields_data['total_products']}")

        # Build lookup by OE number
        extracted_by_oe = {}
        for p in fields_data['products']:
            oe = p.get('oe_number', {}).get('value', '')
            if oe:
                extracted_by_oe[oe] = p

        # Evaluate each annotation
        scores = []
        field_stats = {}
        for ann in gt_data['annotations']:
            pid = ann['product_id']
            found = extracted_by_oe.get(pid)
            if not found:
                # Try partial match
                for oe_key in extracted_by_oe:
                    if pid.lower() in oe_key.lower():
                        found = extracted_by_oe[oe_key]
                        break

            if not found:
                print(f"  [MISS] {pid}: not found in extracted products")
                continue

            score, field_scores, details = evaluate_product(found, ann, rules)
            scores.append(score)

            # Collect field-level stats
            for field, info in field_scores.items():
                if field not in field_stats:
                    field_stats[field] = {'correct': 0, 'total': 0}
                field_stats[field]['total'] += 1
                if info['match']:
                    field_stats[field]['correct'] += 1

            status = 'OK' if score >= 0.8 else ('WARN' if score >= 0.5 else 'FAIL')
            print(f"  [{status}] {pid}: {score:.0%} ", end='')
            for f, d in details.items():
                if not d['match']:
                    print(f"[{f}: {d.get('extracted','?')[:30]} != {d.get('expected', d.get('expected_keywords','?'))[:30]}] ", end='')
            print()

        # Summary
        if scores:
            avg_acc = sum(scores) / len(scores)
            print(f"  ── Summary ──")
            print(f"  Matched products:   {len(scores)}/{len(gt_data['annotations'])}")
            print(f"  Weighted accuracy:  {avg_acc:.0%}")
            for field, stats in field_stats.items():
                field_acc = stats['correct'] / max(stats['total'], 1)
                print(f"    {field:<20}: {field_acc:.0%} ({stats['correct']}/{stats['total']})")
            all_results.append({'file': pdf_name, 'accuracy': avg_acc, 'field_stats': field_stats})

    # Overall summary
    if all_results:
        print(f"\n{'='*70}")
        print(f"  Overall Results")
        print(f"{'='*70}")
        overall_acc = sum(r['accuracy'] for r in all_results) / len(all_results)
        print(f"  Overall weighted accuracy: {overall_acc:.0%}")
        for r in all_results:
            print(f"    {r['file']}.pdf: {r['accuracy']:.0%}")

        # Field-level cross-file summary
        print(f"\n  ── Field-Level Accuracy (across all PDFs) ──")
        all_fields = {}
        for r in all_results:
            for field, stats in r['field_stats'].items():
                if field not in all_fields:
                    all_fields[field] = {'correct': 0, 'total': 0}
                all_fields[field]['correct'] += stats['correct']
                all_fields[field]['total'] += stats['total']
        for field, stats in sorted(all_fields.items()):
            acc = stats['correct'] / max(stats['total'], 1)
            bar = '█' * int(acc * 20)
            print(f"    {field:<20}: {acc:.0%} {bar} ({stats['correct']}/{stats['total']})")

    print(f"\n{'='*70}\n")


if __name__ == '__main__':
    main()
