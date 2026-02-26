# Test Questions for Each Sample File
## All files share the same domain (ACME Supplies / Globex Corp) so cross-file questions also work.

---

## 1. sample_850.edi  (X12 EDI — Purchase Order)

### Opening questions
- "What is this file?"
- "Who is buying from whom, and what are they ordering?"
- "What is the total value of this purchase order?"

### Follow-up / context questions (test memory)
- "What are the payment terms?"
- "How many line items are in the order?"
- "What does the ISA segment do?"
- "Which segment tells me the delivery date?"
- "What does `PE` mean in the PO1 segments?"
- "Can you summarize this PO in one sentence as if you were emailing a warehouse manager?"

---

## 2. sample_orders.edifact  (EDIFACT — Purchase Order)

### Opening questions
- "What type of EDI standard is this and what does it contain?"
- "Who are the sender and receiver in this message?"
- "What items are being ordered and in what quantities?"

### Follow-up / context questions
- "What does UNB stand for and what info does it carry?"
- "What is the currency and total order value?"
- "What does the NAD segment represent?"
- "How is this different from an X12 EDI file?"
- "Is this the same order as the X12 file we looked at earlier?" ← great cross-file memory test if you upload both

---

## 3. sample_catalog.xml  (XML — Product Catalog)

### Opening questions
- "What is this XML file about?"
- "How many products are in this catalog and what are their SKUs?"
- "Which product has the lowest stock relative to its reorder point?"

### Follow-up / context questions
- "What compliance certifications does the motor have?"
- "What are the bulk pricing tiers for the Widget A100?"
- "Which warehouse holds the motor?"
- "What namespaces and schema references are declared in this file?"
- "If I order 600 bolts, what price per pack would I pay?"
- "List all products that are RoHS compliant."

---

## 4. sample_catalog_transform.xslt  (XSLT 2.0 — HTML Transformer)

### Opening questions
- "What does this XSLT file do?"
- "What input XML does this stylesheet expect, and what does it output?"
- "What are all the parameters I can pass to this stylesheet at runtime?"

### Follow-up / context questions
- "What does the `pricingTable` named template do?"
- "How does the stylesheet decide whether to highlight a product as low stock?"
- "What happens to inactive products by default?"
- "Which templates use `xsl:sort` and what are they sorting by?"
- "Can you explain the `$isLowStock` variable logic in plain English?"
- "If I set `showInactiveProducts` to true, what changes in the output?"
- "How would I modify this to also show the product dimensions?"

---

## Cross-file / Continuity Tests (upload files back-to-back)

Upload the XML catalog, ask questions, then upload the XSLT:
- "Does this XSLT match the structure of the XML catalog we just looked at?"
- "Would this stylesheet correctly render the motor product from the catalog?"
- "Are there any fields in the XML that the XSLT doesn't use?"

Upload the X12 EDI, ask questions, then upload the EDIFACT:
- "Are these two files ordering the same products?"
- "What are the key structural differences between the two formats?"
- "Which format would you say is more human-readable and why?"
