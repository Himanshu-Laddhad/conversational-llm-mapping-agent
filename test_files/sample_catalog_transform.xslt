<?xml version="1.0" encoding="UTF-8"?>
<xsl:stylesheet version="2.0"
  xmlns:xsl="http://www.w3.org/1999/XSL/Transform"
  xmlns:xs="http://www.w3.org/2001/XMLSchema"
  exclude-result-prefixes="xs">

  <xsl:output method="html" version="5.0" encoding="UTF-8" indent="yes"/>

  <!-- ============================================================
       GLOBAL PARAMETERS - can be overridden at runtime
  ============================================================ -->
  <xsl:param name="showInactiveProducts" as="xs:boolean" select="false()"/>
  <xsl:param name="highlightLowStock" as="xs:boolean" select="true()"/>
  <xsl:param name="currencySymbol" as="xs:string" select="'$'"/>
  <xsl:param name="maxBulkTiersShown" as="xs:integer" select="3"/>

  <!-- ============================================================
       GLOBAL VARIABLES
  ============================================================ -->
  <xsl:variable name="catalogMeta" select="/ProductCatalog/Metadata"/>
  <xsl:variable name="allCategories" select="/ProductCatalog/Categories/Category"/>
  <xsl:variable name="totalProducts" select="count(/ProductCatalog/Products/Product)"/>

  <!-- ============================================================
       ROOT TEMPLATE - builds the full HTML page
  ============================================================ -->
  <xsl:template match="/">
    <html lang="en">
      <head>
        <meta charset="UTF-8"/>
        <title>
          <xsl:value-of select="concat('Product Catalog - ', /ProductCatalog/@supplier)"/>
        </title>
        <xsl:call-template name="inlineStyles"/>
      </head>
      <body>
        <header>
          <h1><xsl:value-of select="/ProductCatalog/@supplier"/> — Product Catalog</h1>
          <p>Catalog ID: <strong><xsl:value-of select="$catalogMeta/CatalogID"/></strong> |
             Valid Until: <strong><xsl:value-of select="$catalogMeta/PriceValidUntil"/></strong> |
             Currency: <strong><xsl:value-of select="$catalogMeta/Currency"/></strong>
          </p>
          <p>Total Products: <xsl:value-of select="$totalProducts"/></p>
        </header>

        <nav>
          <h2>Categories</h2>
          <ul>
            <xsl:apply-templates select="$allCategories" mode="nav"/>
          </ul>
        </nav>

        <main>
          <xsl:apply-templates select="/ProductCatalog/Products/Product">
            <xsl:sort select="n" order="ascending"/>
          </xsl:apply-templates>
        </main>

        <footer>
          <p>Contact: <a href="mailto:{$catalogMeta/ContactEmail}">
            <xsl:value-of select="$catalogMeta/ContactEmail"/>
          </a></p>
        </footer>
      </body>
    </html>
  </xsl:template>

  <!-- ============================================================
       CATEGORY NAVIGATION ITEM
  ============================================================ -->
  <xsl:template match="Category" mode="nav">
    <li>
      <a href="#{@id}"><xsl:value-of select="@name"/></a>
    </li>
  </xsl:template>

  <!-- ============================================================
       PRODUCT TEMPLATE
       - Skips inactive products unless $showInactiveProducts is true
  ============================================================ -->
  <xsl:template match="Product">
    <xsl:if test="@active = 'true' or $showInactiveProducts">
      <xsl:variable name="categoryName"
        select="$allCategories[@id = current()/@categoryRef]/@name"/>
      <xsl:variable name="isLowStock"
        select="xs:integer(Stock/QuantityOnHand) &lt; xs:integer(Stock/ReorderPoint) * 2"/>

      <article id="{@sku}">
        <xsl:if test="$highlightLowStock and $isLowStock">
          <xsl:attribute name="class">low-stock</xsl:attribute>
        </xsl:if>

        <h2><xsl:value-of select="n"/></h2>
        <p class="meta">SKU: <code><xsl:value-of select="@sku"/></code> |
           Category: <xsl:value-of select="$categoryName"/>
          <xsl:if test="@active = 'false'">
            <span class="badge inactive">INACTIVE</span>
          </xsl:if>
          <xsl:if test="$highlightLowStock and $isLowStock">
            <span class="badge low-stock">LOW STOCK</span>
          </xsl:if>
        </p>

        <p><xsl:value-of select="Description"/></p>

        <!-- Pricing table -->
        <xsl:call-template name="pricingTable">
          <xsl:with-param name="pricingNode" select="Pricing"/>
        </xsl:call-template>

        <!-- Stock info -->
        <xsl:call-template name="stockInfo">
          <xsl:with-param name="stockNode" select="Stock"/>
        </xsl:call-template>

        <!-- Compliance badges if present -->
        <xsl:if test="Compliance">
          <xsl:call-template name="complianceBadges">
            <xsl:with-param name="compNode" select="Compliance"/>
          </xsl:call-template>
        </xsl:if>
      </article>
    </xsl:if>
  </xsl:template>

  <!-- ============================================================
       NAMED TEMPLATE: Pricing Table
  ============================================================ -->
  <xsl:template name="pricingTable">
    <xsl:param name="pricingNode"/>
    <table class="pricing">
      <thead>
        <tr><th>Quantity</th><th>Unit Price</th></tr>
      </thead>
      <tbody>
        <tr>
          <td>1+</td>
          <td><xsl:value-of select="concat($currencySymbol, $pricingNode/UnitPrice)"/></td>
        </tr>
        <xsl:for-each select="$pricingNode/BulkPrice[position() &lt;= $maxBulkTiersShown]">
          <xsl:sort select="@minQty" data-type="number" order="ascending"/>
          <tr>
            <td><xsl:value-of select="concat(@minQty, '+')"/></td>
            <td><xsl:value-of select="concat($currencySymbol, .)"/></td>
          </tr>
        </xsl:for-each>
      </tbody>
    </table>
  </xsl:template>

  <!-- ============================================================
       NAMED TEMPLATE: Stock Info
  ============================================================ -->
  <xsl:template name="stockInfo">
    <xsl:param name="stockNode"/>
    <p class="stock">
      In Stock: <strong><xsl:value-of select="$stockNode/QuantityOnHand"/></strong> units |
      Reorder at: <xsl:value-of select="$stockNode/ReorderPoint"/> |
      Lead time: <xsl:value-of select="$stockNode/LeadTimeDays"/> day(s)
    </p>
  </xsl:template>

  <!-- ============================================================
       NAMED TEMPLATE: Compliance Badges
  ============================================================ -->
  <xsl:template name="complianceBadges">
    <xsl:param name="compNode"/>
    <div class="compliance">
      <xsl:if test="$compNode/RoHS = 'true'"><span class="badge green">RoHS</span></xsl:if>
      <xsl:if test="$compNode/CE = 'true'"><span class="badge green">CE</span></xsl:if>
      <xsl:if test="$compNode/UL = 'true'"><span class="badge green">UL</span></xsl:if>
    </div>
  </xsl:template>

  <!-- ============================================================
       NAMED TEMPLATE: Inline CSS styles
  ============================================================ -->
  <xsl:template name="inlineStyles">
    <style>
      body { font-family: sans-serif; max-width: 960px; margin: auto; padding: 1rem; }
      article { border: 1px solid #ccc; border-radius: 6px; padding: 1rem; margin-bottom: 1.5rem; }
      article.low-stock { border-color: #e07b00; background: #fff8f0; }
      table.pricing { border-collapse: collapse; margin: 0.5rem 0; }
      table.pricing th, table.pricing td { border: 1px solid #ddd; padding: 4px 12px; }
      .badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 0.75rem; margin-left: 6px; }
      .badge.green { background: #d4edda; color: #155724; }
      .badge.inactive { background: #f8d7da; color: #721c24; }
      .badge.low-stock { background: #fff3cd; color: #856404; }
    </style>
  </xsl:template>

</xsl:stylesheet>
